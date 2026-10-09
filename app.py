#!/usr/bin/env python3
"""APCc local UPS control. HTTPS terminates at the dedicated Apache virtual host."""
import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shlex
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

COMMANDS = [
    {"id": "beeper.enable", "label": "Enable audible alarms", "description": "Enable the UPS audible alarms.", "danger": False, "legacy": False},
    {"id": "beeper.disable", "label": "Disable audible alarms", "description": "Disable the UPS audible alarms.", "danger": False, "legacy": False},
    {"id": "beeper.mute", "label": "Mute current alarm", "description": "Temporarily silence the current audible alarm.", "danger": False, "legacy": False},
    {"id": "beeper.on", "label": "Enable alarms · legacy", "description": "Legacy command: prefer beeper.enable.", "danger": False, "legacy": True},
    {"id": "beeper.off", "label": "Disable alarms · legacy", "description": "Legacy command: prefer beeper.disable or beeper.mute.", "danger": False, "legacy": True},
    {"id": "load.off", "label": "Turn off output now", "description": "Immediately cut power to connected devices. This does not shut down their operating systems gracefully.", "danger": True, "legacy": False},
    {"id": "load.off.delay", "label": "Schedule output shutdown", "description": "Request output shutdown using the ups.delay.shutdown delay. This does not shut down operating systems gracefully.", "danger": True, "legacy": False},
    {"id": "shutdown.reboot", "label": "Power cycle output", "description": "Turn output power off and back on. Connected devices will lose power.", "danger": True, "legacy": False},
    {"id": "shutdown.stop", "label": "Cancel pending shutdown", "description": "Request cancellation of a pending shutdown, if the UPS can still cancel it.", "danger": False, "legacy": False},
    {"id": "test.panel.start", "label": "Test beeper and front panel", "description": "Start the front panel test, including indicators and the beeper, when supported by the driver.", "danger": False, "legacy": False},
]
PARAMETERS = [
    {"id": "battery.charge.low", "label": "Low battery threshold", "unit": "%", "min": 1, "max": 99},
    {"id": "battery.runtime.low", "label": "Low runtime threshold", "unit": "s", "min": 30, "max": 7200},
    {"id": "ups.delay.shutdown", "label": "Output shutdown delay", "unit": "s", "min": 0, "max": 3600},
]
COMMAND_MAP = {x["id"]: x for x in COMMANDS}
PARAMETER_MAP = {x["id"]: x for x in PARAMETERS}
COOKIE_NAME = "apcc_session"
STATIC = Path(__file__).resolve().parent / "static"
VERSION = "3.0.0"
SETTING_KEYS = {"display_name", "expected_model", "device_ip", "nut_host", "nut_port", "ups", "expected_serial", "nut_username", "nut_password", "poll_interval"}


def default_outlets():
    return [{"id": "outlet-" + str(i), "connector": "Unspecified", "label": "Outlet " + str(i), "device": "", "estimated_w": None} for i in range(1, 10)]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def request_identifier(data):
    value = data.get("request_id", "")
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", value):
        raise ApiError(400, "Invalid request identifier")
    return value


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class NutError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def quote(value):
    text = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("Control characters are not allowed")
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


class NutClient:
    def __init__(self, config):
        self.config = config
        self.ups = config.get("ups", "apc")
        self.sock = None
        self.reader = None

    def __enter__(self):
        self.sock = socket.create_connection((self.config.get("nut_host", "127.0.0.1"), int(self.config.get("nut_port", 3493))), timeout=4)
        self.reader = self.sock.makefile("rb")
        return self

    def __exit__(self, *exc):
        if self.reader:
            self.reader.close()
        if self.sock:
            self.sock.close()

    def _line(self):
        raw = self.reader.readline(65537)
        if not raw or len(raw) > 65536 or not raw.endswith(b"\n"):
            raise NutError("NUT response interrupted or too large")
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if text.startswith("ERR "):
            code = text[4:]
            if not re.fullmatch(r"[A-Z0-9_-]{1,80}", code):
                code = "PROTOCOL-ERROR"
            raise NutError("NUT: " + code)
        try:
            return shlex.split(text)
        except ValueError as exc:
            raise NutError("Invalid NUT response") from exc

    def _send(self, line):
        self.sock.sendall((line + "\n").encode("utf-8"))

    def _ok(self, line):
        self._send(line)
        result = self._line()
        if not result or result[0] != "OK":
            raise NutError("NUT did not acknowledge the request")

    def _list(self, kind):
        self._send("LIST " + kind + " " + quote(self.ups))
        if self._line() != ["BEGIN", "LIST", kind, self.ups]:
            raise NutError("Invalid NUT list header")
        rows = []
        for _ in range(2048):
            row = self._line()
            if row == ["END", "LIST", kind, self.ups]:
                return rows
            if len(row) < 3 or row[:2] != [kind, self.ups]:
                raise NutError("Invalid NUT list item")
            rows.append(row)
        raise NutError("NUT list is too large")

    def snapshot(self):
        variables = {row[2]: row[3] for row in self._list("VAR") if len(row) == 4}
        commands = [row[2] for row in self._list("CMD") if len(row) == 3]
        writable = [row[2] for row in self._list("RW") if len(row) == 4]
        if "ups.status" not in variables:
            raise NutError("UPS status unavailable")
        return {"variables": variables, "supported_commands": commands, "supported_variables": writable}

    def _authenticate(self):
        self._ok("USERNAME " + quote(self.config["nut_username"]))
        self._ok("PASSWORD " + quote(self.config["nut_password"]))

    def command(self, command, extra=None):
        if command not in COMMAND_MAP:
            raise ValueError("Command not allowed")
        if extra is not None and (command != "load.off.delay" or not re.fullmatch(r"[0-9]{1,4}", str(extra)) or not 0 <= int(extra) <= 3600):
            raise ValueError("Invalid delay")
        self._authenticate()
        self._ok("INSTCMD " + quote(self.ups) + " " + quote(command) + (" " + quote(extra) if extra is not None else ""))

    def set_variable(self, variable, value):
        if variable not in PARAMETER_MAP or not re.fullmatch(r"[0-9]{1,5}", str(value)):
            raise ValueError("Parameter not allowed")
        self._authenticate()
        self._ok("SET VAR " + quote(self.ups) + " " + quote(variable) + " " + quote(value))


class Application:
    def __init__(self, config):
        self.config = dict(config)
        self.settings_path = Path(config.get("settings_path", str(Path(config["audit_path"]).parent / "settings.json")))
        self.auth_path = Path(config.get("auth_path", str(Path(config["audit_path"]).parent / "auth.json")))
        if self.settings_path.exists():
            overlay = json.loads(self.settings_path.read_text())
            self.config.update({k: v for k, v in overlay.items() if k in SETTING_KEYS or k == "outlets"})
        if self.auth_path.exists():
            overlay = json.loads(self.auth_path.read_text())
            self.config.update({k: overlay[k] for k in ("username", "password_salt", "password_hash", "password_iterations")})
        self.config.setdefault("display_name", "Your UPS")
        self.config.setdefault("device_ip", "")
        self.config.setdefault("poll_interval", 15)
        self.config.setdefault("outlets", default_outlets())
        self.nut_factory = lambda: NutClient(self.config.copy())
        self.config_nut_factory = lambda candidate: NutClient(candidate)
        self.last_snapshot = None
        self.sessions = {}
        self.failures = {}
        self.session_lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.audit_lock = threading.Lock()
        self.audit_path = Path(config["audit_path"])
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.audit_path) + ".sqlite3"
        with self.database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status INTEGER, response TEXT, created REAL NOT NULL)")
        self.audit_rows = collections.deque(maxlen=100)
        if self.audit_path.exists():
            # Read only the tail of a growing log on service startup.
            with self.audit_path.open("rb") as fh:
                fh.seek(max(0, self.audit_path.stat().st_size - 131072))
                for line in fh:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            self.audit_rows.append(row)
                    except (ValueError, UnicodeError):
                        pass

    @contextlib.contextmanager
    def database(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, actor, action, target, result):
        row = {"time": utcnow(), "actor": actor, "action": action, "target": target, "result": result}
        with self.audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            self.audit_rows.append(row)
        logging.info("audit actor=%s action=%s target=%s result=%s", actor, action, target, result)

    def authenticate(self, username, password, client):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 100 or len(password) > 1024:
            raise ApiError(400, "Invalid credentials")
        now = time.time()
        # Include a global account bucket: an attacker cannot evade by varying username or forwarded IP.
        buckets = ["global", "ip:" + client]
        with self.session_lock:
            self.failures = {k: [x for x in v if now - x < 300] for k, v in self.failures.items() if any(now - x < 300 for x in v)}
            for key in buckets:
                if len(self.failures.get(key, [])) >= (30 if key == "global" else 8):
                    raise ApiError(429, "Too many attempts. Wait five minutes.")
            if len(self.failures) > 1000:
                raise ApiError(429, "Too many attempts. Wait five minutes.")
        with self.session_lock:
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self.config["password_salt"]), self.config.get("password_iterations", 600000)).hex()
            valid = hmac.compare_digest(username.encode(), self.config["username"].encode()) and hmac.compare_digest(candidate, self.config["password_hash"])
            if not valid:
                for key in buckets:
                    self.failures.setdefault(key, []).append(now)
                self.audit("anonymous", "login", "panel", "denied")
                raise ApiError(401, "Incorrect username or password")
            self.failures.pop("ip:" + client, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            if len(self.sessions) >= 500:
                raise ApiError(429, "Too many active sessions")
            token = secrets.token_urlsafe(32)
            session = {"username": username, "csrf": secrets.token_urlsafe(32), "expires": now + 3600}
            self.sessions[token] = session
        self.audit(username, "login", "panel", "accepted")
        return token, session

    def session(self, token):
        with self.session_lock:
            session = self.sessions.get(token)
            if not session or session["expires"] < time.time():
                self.sessions.pop(token, None)
                return None
            return session.copy()

    @staticmethod
    def beeper_capability(snapshot=None):
        if snapshot and "test.panel.start" in snapshot.get("supported_commands", []):
            return {"supported": True, "command": "test.panel.start", "reason": "Test the UPS front panel indicators and audible alarm."}
        return {"supported": False, "command": None, "reason": "The current driver does not expose an audible test. Enabling alarms does not produce a test beep."}

    def config_public(self):
        with self.operation_lock:
            result = {k: self.config.get(k, "") for k in SETTING_KEYS if k != "nut_password"}
            result.update(username=self.config["username"], has_nut_password=bool(self.config.get("nut_password")), beeper_test=self.beeper_capability(self.last_snapshot), outlets=self.config["outlets"], version=VERSION)
            return result

    @staticmethod
    def text_field(value, label, maximum=100, allow_empty=True):
        if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ApiError(400, label + ": invalid value")
        value = value.strip()
        if not value and not allow_empty:
            raise ApiError(400, label + ": required field")
        return value

    def validate_settings(self, data):
        if set(data) - SETTING_KEYS - {"request_id"}:
            raise ApiError(400, "The request contains settings that cannot be changed")
        result = {k: self.config.get(k, "") for k in SETTING_KEYS}
        for key, value in data.items():
            if key in SETTING_KEYS and not (key == "nut_password" and value == ""):
                result[key] = value
        for key, label in (("display_name", "UPS name"), ("expected_model", "Model"), ("expected_serial", "Serial number"), ("ups", "NUT UPS name"), ("nut_username", "NUT username")):
            result[key] = self.text_field(result[key], label, allow_empty=key == "expected_serial")
        for key in ("ups", "nut_username"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", result[key]):
                raise ApiError(400, key + ": use letters, numbers, periods, hyphens or underscores")
        password = result["nut_password"]
        if not isinstance(password, str) or not 1 <= len(password) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in password):
            raise ApiError(400, "NUT password: invalid value")
        for key, low, high in (("nut_port", 1, 65535), ("poll_interval", 5, 300)):
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]{1,5}", str(value)) or not low <= int(value) <= high:
                raise ApiError(400, key + ": allowed range " + str(low) + "–" + str(high))
            result[key] = int(value)
        for key in ("nut_host", "device_ip"):
            result[key] = self.text_field(result[key], key)
            if key == "device_ip" and not result[key]:
                continue
            if key == "nut_host" and result[key] == "localhost":
                result[key] = "127.0.0.1"
            try:
                address = ipaddress.ip_address(result[key])
            except ValueError:
                raise ApiError(400, key + ": enter a valid IP address")
            if address.is_unspecified or address.is_multicast or address.is_link_local:
                raise ApiError(400, key + ": unusable address")
            if key == "nut_host":
                networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
                if not any(address in ipaddress.ip_network(network) for network in networks):
                    raise ApiError(400, "The NUT server must be on a private network or localhost")
            result[key] = str(address)
        return result

    def test_config(self, data):
        candidate = self.validate_settings(data)
        try:
            with self.config_nut_factory(candidate) as nut:
                # NUT USERNAME/PASSWORD only store credentials; they do not verify
                # command privileges. Keep this connection probe entirely read-only.
                snapshot = nut.snapshot()
                serial = (snapshot["variables"].get("device.serial") or snapshot["variables"].get("ups.serial") or "").strip()
                if candidate["expected_serial"] and serial != candidate["expected_serial"]:
                    raise ApiError(409, "Reported serial (" + serial + ") does not match the configured serial")
        except (NutError, OSError) as exc:
            raise ApiError(503, "NUT connection failed: " + str(exc)) from exc
        return {"message": "Connection and UPS identity verified. Command permissions will be checked when a command is executed.", "credentials_verified": False, "serial": serial, "model": snapshot["variables"].get("device.model", snapshot["variables"].get("ups.model", "")), "commands": snapshot["supported_commands"], "variables": snapshot["supported_variables"]}

    def persist_settings(self):
        atomic_json(self.settings_path, {k: self.config[k] for k in SETTING_KEYS | {"outlets"} if k in self.config})

    def save_config(self, actor, data):
        request_identifier(data)
        with self.operation_lock:
            candidate = self.validate_settings(data)
            connection_keys = ("nut_host", "nut_port", "ups", "expected_serial", "nut_username", "nut_password")
            changed = [key for key in candidate if candidate[key] != self.config.get(key)]
            if any(key in changed for key in connection_keys):
                tested = self.test_config(data)
                if not tested["serial"]:
                    raise ApiError(409, "The new device does not report a serial number: its identity cannot be verified")
                candidate["expected_serial"] = tested["serial"]
            prior = self.config.copy()
            self.config.update(candidate)
            try:
                self.persist_settings()
            except OSError:
                self.config = prior
                raise ApiError(500, "Configuration not saved; previous settings retained")
            self.last_snapshot = None
            self.audit(actor, "configuration", ",".join(k for k in changed if k != "nut_password") or "panel", "saved")
            return {"message": "Configuration saved. The physical UPS network settings were not changed.", "config": self.config_public()}

    def change_password(self, actor, data):
        request_identifier(data)
        current, new = data.get("current_password"), data.get("new_password")
        if not isinstance(current, str) or len(current) > 1024 or not isinstance(new, str) or not 8 <= len(new) <= 128:
            raise ApiError(400, "The new password must contain 8 to 128 characters")
        with self.operation_lock, self.session_lock:
            candidate = hashlib.pbkdf2_hmac("sha256", current.encode(), bytes.fromhex(self.config["password_salt"]), self.config.get("password_iterations", 600000)).hex()
            if not hmac.compare_digest(candidate, self.config["password_hash"]):
                raise ApiError(403, "Incorrect current password")
            salt = secrets.token_hex(32)
            auth = {"username": actor, "password_salt": salt, "password_iterations": 600000, "password_hash": hashlib.pbkdf2_hmac("sha256", new.encode(), bytes.fromhex(salt), 600000).hex()}
            atomic_json(self.auth_path, auth)
            self.config.update(auth)
            self.sessions.clear()
            self.audit(actor, "password", "account", "changed_sessions_revoked")
        return {"message": "Password updated. Sign in again with your new password."}

    def save_outlets(self, actor, data):
        request_identifier(data)
        entries = data.get("outlets")
        current = {item["id"]: item for item in self.config["outlets"]}
        if not isinstance(entries, list) or len(entries) != len(current):
            raise ApiError(400, "Include every configured outlet")
        seen, clean = set(), []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] not in current or entry["id"] in seen:
                raise ApiError(400, "Invalid outlet identifiers")
            seen.add(entry["id"])
            label = self.text_field(entry.get("label", ""), "Outlet name", 80, False)
            device = self.text_field(entry.get("device", ""), "Connected device", 100)
            watts = entry.get("estimated_w")
            if watts == "":
                watts = None
            if watts is not None:
                if isinstance(watts, bool):
                    raise ApiError(400, "Invalid power estimate")
                try:
                    watts = float(watts)
                except (TypeError, ValueError):
                    raise ApiError(400, "Invalid power estimate")
                if not math.isfinite(watts) or not 0 <= watts <= 100000:
                    raise ApiError(400, "Allowed power estimate: 0–100000 W")
            clean.append({"id": entry["id"], "connector": current[entry["id"]]["connector"], "label": label, "device": device, "estimated_w": watts})
        with self.operation_lock:
            old = self.config["outlets"]
            self.config["outlets"] = clean
            try:
                self.persist_settings()
            except OSError:
                self.config["outlets"] = old
                raise ApiError(500, "Outlet inventory not saved")
            self.audit(actor, "outlets", "inventory", "saved_estimates")
        return {"message": "Devices and estimates saved. No UPS settings were changed.", "outlets": clean}

    def consumption(self, snapshot):
        values = snapshot["variables"]
        def measured(*keys):
            for key in keys:
                try:
                    value = float(values[key])
                    if math.isfinite(value) and value >= 0:
                        return value
                except (KeyError, TypeError, ValueError):
                    pass
            return None
        outlets = self.config["outlets"]
        estimates = [outlet["estimated_w"] for outlet in outlets if outlet["estimated_w"] is not None]
        return {"measured_total_w": measured("ups.realpower", "output.realpower"), "apparent_power_va": measured("ups.power"), "load_percent": measured("ups.load"), "estimated_total_w": sum(estimates) if estimates else None, "estimated_count": len(estimates), "outlet_count": len(outlets), "per_outlet_metering": False, "outlets": outlets}

    def beeper_test(self, actor, data):
        request_identifier(data)
        with self.operation_lock:
            try:
                with self.nut_factory() as nut:
                    snapshot = nut.snapshot()
                    self.verify_identity(snapshot)
            except (NutError, OSError) as exc:
                raise ApiError(503, "UPS unreachable: " + str(exc)) from exc
            capability = self.beeper_capability(snapshot)
            if not capability["supported"]:
                raise ApiError(409, capability["reason"])
            return self.mutate(actor, "command", {"command": capability["command"], "request_id": data["request_id"]})

    def verify_identity(self, snapshot):
        expected = self.config.get("expected_serial", "")
        variables = snapshot["variables"]
        actual = (variables.get("device.serial") or variables.get("ups.serial") or "").strip()
        if expected and actual != expected:
            raise ApiError(409, "The UPS serial does not match the configured identity. Controls are blocked.")

    def status(self):
        with self.operation_lock:
            return self._status()

    def _status(self):
        try:
            with self.nut_factory() as nut:
                result = nut.snapshot()
        except (NutError, OSError) as exc:
            raise ApiError(503, "UPS unreachable: " + str(exc)) from exc
        self.verify_identity(result)
        self.last_snapshot = result
        with self.audit_lock:
            audit = list(reversed(self.audit_rows))[:30]
        result.update(ups=self.config.get("ups", "apc"), model=self.config.get("expected_model", "UPS"), display_name=self.config["display_name"], device_ip=self.config["device_ip"], endpoint=self.config.get("ups", "apc") + "@" + self.config.get("nut_host", "127.0.0.1") + ":" + str(self.config.get("nut_port", 3493)), poll_interval=self.config["poll_interval"], beeper_test=self.beeper_capability(result), consumption=self.consumption(result), commands=COMMANDS, parameters=PARAMETERS, audit=audit, checked_at=utcnow(), version=VERSION)
        return result

    def mutate(self, actor, kind, data):
        request_id = data.get("request_id", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", request_id):
            raise ApiError(400, "Invalid request identifier")
        if kind == "command":
            target = data.get("command")
            if not isinstance(target, str) or target not in COMMAND_MAP:
                raise ApiError(400, "Command not allowed")
            if COMMAND_MAP[target]["danger"] and data.get("confirmation") != "POWER OFF":
                raise ApiError(400, "Type POWER OFF to confirm the power interruption")
            value = None
        else:
            target = data.get("variable")
            if not isinstance(target, str) or target not in PARAMETER_MAP:
                raise ApiError(400, "Parameter not allowed")
            value = data.get("value")
            if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)):
                raise ApiError(400, "Enter a valid whole number")
            limits = PARAMETER_MAP[target]
            if not limits["min"] <= int(value) <= limits["max"]:
                raise ApiError(400, "Value outside the allowed range: %s–%s %s" % (limits["min"], limits["max"], limits["unit"]))
            value = str(int(value))
        fingerprint = hashlib.sha256(json.dumps([actor, kind, target, value]).encode()).hexdigest()
        with self.operation_lock:
            with self.database() as db:
                old = db.execute("SELECT fingerprint, status, response FROM operations WHERE id=?", (request_id,)).fetchone()
                if old:
                    if old[0] != fingerprint:
                        raise ApiError(409, "Identifier already used for a different request")
                    if old[1] is None:
                        raise ApiError(409, "The previous request has an uncertain outcome. Check the UPS before sending another action.")
                    return old[1], json.loads(old[2])
            dispatched = False
            try:
                with self.nut_factory() as nut:
                    snapshot = nut.snapshot()
                    self.verify_identity(snapshot)
                    capability = "supported_commands" if kind == "command" else "supported_variables"
                    if target not in snapshot[capability]:
                        raise ApiError(400, "This function is unavailable on the current driver")
                    if not snapshot["variables"].get("ups.status", "").strip():
                        raise ApiError(503, "UPS status unavailable: action blocked")
                    # Commit before sending: a crash or a timeout must never trigger an automatic replay.
                    with self.database() as db:
                        db.execute("INSERT INTO operations VALUES (?,?,NULL,NULL,?)", (request_id, fingerprint, time.time()))
                    self.audit(actor, kind, target + ("=" + value if value is not None else ""), "dispatching")
                    try:
                        dispatched = True
                        if kind == "command":
                            if target == "load.off.delay":
                                delay = str(snapshot["variables"].get("ups.delay.shutdown", ""))
                                if not re.fullmatch(r"[0-9]{1,4}", delay) or not 0 <= int(delay) <= 3600:
                                    raise NutError("Invalid UPS delay: command not sent")
                                nut.command(target, extra=delay)
                            else:
                                nut.command(target)
                        else:
                            nut.set_variable(target, value)
                        status = 200
                        response = {"accepted": True, "message": "Request accepted by NUT. Physical execution is not verified; check the UPS status."}
                        audit_result = "accepted_by_nut"
                    except NutError as exc:
                        status, response = 502, {"error": str(exc) + ". Check the UPS status before retrying.", "accepted": False}
                        audit_result = "nut_error"
                    except OSError:
                        status, response = 502, {"error": "Connection interrupted: outcome uncertain. Do not retry automatically; check the UPS.", "accepted": False}
                        audit_result = "uncertain"
                    try:
                        self.audit(actor, kind, target + ("=" + value if value is not None else ""), audit_result)
                    except OSError:
                        logging.exception("Audit write failed after dispatch; outcome retained in the request ledger")
            except (NutError, OSError) as exc:
                if dispatched:
                    raise ApiError(502, "Outcome uncertain after dispatch. Check the UPS before retrying.") from exc
                raise ApiError(503, "UPS unreachable; request not sent: " + str(exc)) from exc
            with self.database() as db:
                db.execute("UPDATE operations SET status=?,response=? WHERE id=?", (status, json.dumps(response), request_id))
            return status, response


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "APCc"
        sys_version = ""
        timeout = 10

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            # Do not log request bodies, session IDs or credentials.
            logging.info("http %s %s", self.command, self.path.split("?")[0][:120])

        def respond(self, status, payload, content_type="application/json; charset=utf-8", cookie=None):
            content = json.dumps(payload, ensure_ascii=False).encode() if content_type.startswith("application/json") else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(content)

        def get_session(self, required=True):
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
                token = jar[COOKIE_NAME].value if COOKIE_NAME in jar else ""
            except http.cookies.CookieError:
                token = ""
            session = app.session(token)
            if required and not session:
                raise ApiError(401, "Sign in to APCc")
            return token, session

        def verify_origin(self):
            # This header is overwritten by Apache, and the backend binds loopback only.
            if self.headers.get("X-Forwarded-Proto") != "https":
                raise ApiError(403, "Use the APCc HTTPS address")
            host = self.headers.get("Host", "")
            if not host or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
                raise ApiError(403, "Invalid host")
            if self.headers.get("Origin") != "https://" + host:
                raise ApiError(403, "Request origin not allowed")

        def json_body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "Unsupported request format")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise ApiError(415, "JSON request required")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise ApiError(400, "Invalid request size")
            if not 0 < length <= 8192:
                raise ApiError(413, "Request too large or empty")
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise ApiError(400, "Invalid JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "JSON object required")
            return data

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path == "/api/session":
                    _, session = self.get_session(False)
                    return self.respond(200, {"authenticated": bool(session), **({"csrf": session["csrf"], "username": session["username"]} if session else {})})
                if path == "/api/status":
                    self.get_session()
                    return self.respond(200, app.status())
                if path == "/api/config":
                    self.get_session()
                    return self.respond(200, app.config_public())
                files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
                files["/logo.svg"] = ("logo.svg", "image/svg+xml")
                files["/favicon.svg"] = files["/logo.svg"]
                files["/static/app.js"] = files["/app.js"]
                files["/static/style.css"] = files["/style.css"]
                if path not in files:
                    raise ApiError(404, "Page not found")
                name, content_type = files[path]
                return self.respond(200, (STATIC / name).read_bytes(), content_type)
            except ApiError as exc:
                self.respond(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logging.exception("Error while reading the request")
                self.respond(500, {"error": "Internal APCc error"})

        def authenticated_post(self, path, data):
            token, session = self.get_session()
            csrf = self.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf, session["csrf"]):
                raise ApiError(403, "Invalid session or security token. Reload the page.")
            if path == "/api/logout":
                with app.session_lock:
                    app.sessions.pop(token, None)
                return self.respond(200, {"message": "Signed out"}, cookie=COOKIE_NAME + "=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0")
            if path == "/api/config/test":
                return self.respond(200, app.test_config(data))
            if path == "/api/config":
                return self.respond(200, app.save_config(session["username"], data))
            if path == "/api/password":
                return self.respond(200, app.change_password(session["username"], data), cookie=COOKIE_NAME + "=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0")
            if path == "/api/outlets":
                return self.respond(200, app.save_outlets(session["username"], data))
            if path == "/api/beeper-test":
                status, response = app.beeper_test(session["username"], data)
                return self.respond(status, response)
            if path not in ("/api/action", "/api/setting"):
                raise ApiError(404, "Function not found")
            status, response = app.mutate(session["username"], "command" if path == "/api/action" else "setting", data)
            self.respond(status, response)

        def do_POST(self):
            try:
                self.verify_origin()
                data = self.json_body()
                path = urlsplit(self.path).path
                if path == "/api/login":
                    client = self.headers.get("X-Panel-Client-IP", self.client_address[0])[:100]
                    token, session = app.authenticate(data.get("username"), data.get("password"), client)
                    cookie = COOKIE_NAME + "=" + token + "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600"
                    return self.respond(200, {"username": session["username"], "csrf": session["csrf"]}, cookie=cookie)
                with app.operation_lock:
                    return self.authenticated_post(path, data)
            except ApiError as exc:
                self.respond(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logging.exception("Error while handling the request")
                self.respond(500, {"error": "Internal error: the outcome may be uncertain. Check the UPS before retrying."})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", config.get("ups", "apc")):
        raise SystemExit("Invalid UPS identifier")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = Application(config)
    server = ThreadingHTTPServer(("127.0.0.1", int(config.get("listen_port", 8765))), make_handler(app))
    server.daemon_threads = True
    logging.info("APCc listening on loopback; use the HTTPS proxy")
    server.serve_forever()


if __name__ == "__main__":
    main()
