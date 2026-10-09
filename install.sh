#!/usr/bin/env bash
# Local installation on Debian, Ubuntu, and Linux Mint. No UPS commands are sent.
set -Eeuo pipefail
umask 077

RESET_ADMIN=0
ROTATE_NUT_PASSWORD=0
while (($#)); do
  case "$1" in
    --reset-admin) RESET_ADMIN=1 ;;
    --rotate-nut-password) ROTATE_NUT_PASSWORD=1 ;;
    --help|-h)
      printf 'Usage: sudo bash install.sh [--reset-admin] [--rotate-nut-password]\n'
      printf 'First install: a random admin password is generated. Reinstallation preserves credentials and settings.\n'
      printf '%s\n' '--reset-admin: generate a new admin password and revoke sessions on restart.'
      printf '%s\n' '--rotate-nut-password: replace the password of the local apcc NUT account.'
      printf 'APCC_UPS: local NUT UPS name (default apc; reinstallation uses the saved name).\n'
      printf 'APCC_ALLOWED_CIDR: allowed networks separated by spaces or commas (default loopback only).\n'
      exit 0 ;;
    *) printf 'Unknown argument: %s; use --help.\n' "$1" >&2; exit 2 ;;
  esac
  shift
done
[[ $(uname -s) == Linux ]] || { printf 'This package requires Debian, Ubuntu, or Linux Mint.\n' >&2; exit 1; }
[[ $EUID -eq 0 ]] || { printf 'Run sudo bash install.sh.\n' >&2; exit 1; }
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=/opt/apcc
CONFIG_DIR=/etc/apcc
CONFIG=$CONFIG_DIR/config.json
STATE_DIR=/var/lib/apcc
NUT_USERS=/etc/nut/upsd.users
SITE=/etc/apache2/sites-available/apcc.conf
LISTENER=/etc/apache2/conf-available/apcc-listen.conf
SITE_LINK=/etc/apache2/sites-enabled/apcc.conf
LISTENER_LINK=/etc/apache2/conf-enabled/apcc-listen.conf
STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$$
WORK=$(mktemp -d /tmp/apcc-install.XXXXXXXX)
trap 'rm -rf -- "$WORK"' EXIT
trap 'printf "Installation stopped at line %s; check the preceding error.\n" "$LINENO" >&2' ERR
die() { printf '%s\n' "$*" >&2; exit 1; }

for command in apt-get dpkg-query systemctl; do
  command -v "$command" >/dev/null || die 'Debian, Ubuntu, or Linux Mint with apt and systemd is required.'
done
[[ -f "$SOURCE_DIR/app.py" && -d "$SOURCE_DIR/static" ]] || die 'Incomplete archive: app.py and static/ must be next to install.sh.'
[[ -f "$NUT_USERS" ]] || die 'Missing /etc/nut/upsd.users: configure NUT and your UPS first.'
command -v upsc >/dev/null || die 'Missing upsc: install and configure NUT before APCc.'
command -v flock >/dev/null || die 'Missing flock (util-linux package).'
exec 9>/run/lock/apcc-install.lock
flock -n 9 || die "Another APCc installation is in progress."

missing=()
for package in python3 apache2 openssl; do
  if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then
    missing+=("$package")
  fi
done
if ((${#missing[@]})); then
  apt-get update
  apt-get install -y -- "${missing[@]}"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10 or later is required.")'
UPS_NAME=$(python3 - "$CONFIG" <<'PY'
import json, os, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit('config.json must not be a symbolic link.')
config = json.loads(path.read_text()) if path.exists() else {}
name = os.environ.get('APCC_UPS', config.get('ups', 'apc'))
if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
    raise SystemExit('Invalid APCC_UPS: use letters, numbers, periods, hyphens, or underscores.')
if config and config.get('ups') != name:
    raise SystemExit('The UPS differs from the saved configuration. Verify its identity and connection before changing it in the dashboard.')
print(name)
PY
)
upsc "$UPS_NAME@127.0.0.1" >"$WORK/upsc.txt" || die "UPS $UPS_NAME is unavailable at 127.0.0.1; fix NUT before proceeding."
if [[ ! ${APCC_ALLOWED_CIDR+x} && -f "$SITE" ]] && grep -Fxq '# Managed by apcc installer' "$SITE"; then
  APCC_ALLOWED_CIDR=$(python3 - "$SITE" <<'PY'
import pathlib, re, sys
matches = re.findall(r'^\s*Require ip\s+([^\r\n]+)$', pathlib.Path(sys.argv[1]).read_text(), re.M)
if len(matches) != 1:
    raise SystemExit('Existing site ACL is not recognized: specify APCC_ALLOWED_CIDR.')
print(matches[0])
PY
)
fi
export APCC_ALLOWED_CIDR="${APCC_ALLOWED_CIDR-127.0.0.1 ::1}"
python3 - <<'PY' >"$WORK/allowed.txt"
import ipaddress, os
values = os.environ['APCC_ALLOWED_CIDR'].replace(',', ' ').split()
if not values:
    raise SystemExit('APCC_ALLOWED_CIDR must not be empty.')
networks = [ipaddress.ip_network(value, strict=False) for value in values]
if any(network.prefixlen == 0 for network in networks):
    raise SystemExit('A /0 network would expose the dashboard to everyone: specify management networks.')
print(' '.join(dict.fromkeys(['127.0.0.1', '::1'] + [str(network) for network in networks])))
PY
ALLOWED=$(cat "$WORK/allowed.txt")

# Fail closed if someone else owns these Apache configuration files or port 8443.
for path in "$SITE" "$LISTENER"; do
  if [[ -e "$path" ]]; then
    [[ -f "$path" && ! -L "$path" ]] || die "Unexpected Apache path: $path"
    grep -Fxq '# Managed by apcc installer' "$path" || die "Existing configuration is not managed by APCc: $path"
  fi
done
for pair in "$SITE_LINK:$SITE" "$LISTENER_LINK:$LISTENER"; do
  link=${pair%%:*}; target=${pair#*:}
  if [[ -e "$link" || -L "$link" ]]; then
    [[ -L "$link" && $(readlink -f "$link") == "$target" ]] || die "Unexpected enabled Apache configuration: $link"
  fi
done
python3 - "$SITE" "$LISTENER" <<'PY'
import pathlib, subprocess, sys
def listeners(port):
    result = set()
    for name in ('tcp', 'tcp6'):
        for row in pathlib.Path('/proc/net/' + name).read_text().splitlines()[1:]:
            fields = row.split()
            if int(fields[1].rsplit(':', 1)[1], 16) == port and fields[3] == '0A':
                result.add(fields[9])
    return result
backend = listeners(8765)
if backend:
    status = subprocess.run(['systemctl', 'show', 'apcc.service', '--property=MainPID', '--value'],
                            capture_output=True, text=True)
    pid = status.stdout.strip()
    owned = set()
    if pid.isdigit() and int(pid) > 0:
        try:
            for descriptor in pathlib.Path('/proc', pid, 'fd').iterdir():
                try:
                    destination = descriptor.readlink().as_posix()
                except OSError:
                    continue
                if destination.startswith('socket:['):
                    owned.add(destination[8:-1])
        except OSError:
            pass
    if not backend.issubset(owned):
        raise SystemExit('Port 8765 is already used by another service: installation stopped.')
inodes = listeners(8443)
if inodes:
    if not all(pathlib.Path(path).is_file() for path in sys.argv[1:]):
        raise SystemExit('Port 8443 is already in use; APCc will not modify its existing listener.')
    found = set()
    for process in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            if (process / 'comm').read_text().strip() != 'apache2':
                continue
            for descriptor in (process / 'fd').iterdir():
                try:
                    destination = descriptor.readlink().as_posix()
                except OSError:
                    continue
                if destination.startswith('socket:['):
                    found.add(destination[8:-1])
        except (OSError, PermissionError):
            continue
    if not inodes.issubset(found):
        raise SystemExit('Port 8443 is used by a service other than Apache: installation stopped.')
PY

if ! getent group apcc >/dev/null; then
  addgroup --system apcc
fi
if ! id apcc >/dev/null 2>&1; then
  adduser --system --ingroup apcc --home "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin apcc
fi
[[ $(id -u apcc) -ne 0 ]] || die "The apcc account must not be root."
install -d -o root -g apcc -m 0750 "$CONFIG_DIR"
install -d -o apcc -g apcc -m 0700 "$STATE_DIR"
[[ ! -L "$CONFIG" ]] || die 'config.json must not be a symbolic link.'

# Stop the writer before migrating mutable settings/authentication state. The
# service is restarted below after Apache validation, revoking all old sessions.
if systemctl is-active --quiet apcc.service; then
  systemctl stop apcc.service
fi
python3 - "$CONFIG" "$RESET_ADMIN" "$ROTATE_NUT_PASSWORD" "$WORK/upsc.txt" "$STATE_DIR" "$UPS_NAME" "$WORK/admin-password" <<'PY'
import grp, hashlib, json, os, pathlib, pwd, re, secrets, stat, sys, tempfile
path = pathlib.Path(sys.argv[1])
reset = sys.argv[2] == '1'
rotate = sys.argv[3] == '1'
state = pathlib.Path(sys.argv[5])
ups_name = sys.argv[6]
password_notice = pathlib.Path(sys.argv[7])
values = dict(line.split(': ', 1) for line in pathlib.Path(sys.argv[4]).read_text().splitlines() if ': ' in line)
observed_serial = values.get('ups.serial', values.get('device.serial', '')).strip()
observed_model = values.get('ups.model', values.get('device.model', '')).strip()
if not observed_serial:
    raise SystemExit('The driver does not report a UPS serial number: identity verification cannot be configured.')
gid = grp.getgrnam('apcc').gr_gid
uid = pwd.getpwnam('apcc').pw_uid
existing = path.exists()
if existing:
    config = json.loads(path.read_text())
    required = ('nut_host', 'nut_port', 'ups', 'nut_username', 'nut_password',
                'username', 'password_salt', 'password_hash', 'password_iterations',
                'listen_port', 'audit_path', 'expected_model', 'expected_serial')
    if any(key not in config for key in required):
        raise SystemExit('Existing configuration is incomplete and was not overwritten. Check config.json.')
    if (config['nut_host'], config['nut_port'], config['ups'], config['nut_username'],
        config['listen_port']) != ('127.0.0.1', 3493, ups_name, 'apcc', 8765):
        raise SystemExit('Base configuration differs from the expected local settings and was not modified.')
    if config['expected_serial'] != observed_serial:
        raise SystemExit('UPS serial number differs from the saved identity: configuration not modified.')
    if len(config['nut_password']) != 64 or any(c not in '0123456789abcdef' for c in config['nut_password']):
        raise SystemExit('Existing NUT password has an unexpected format and was not modified.')
else:
    config = dict(nut_host='127.0.0.1', nut_port=3493, ups=ups_name,
                  nut_username='apcc', nut_password=secrets.token_hex(32),
                  username='admin', password_iterations=600000, listen_port=8765,
                  audit_path='/var/lib/apcc/audit.jsonl',
                  expected_model=observed_model, expected_serial=observed_serial)
config.setdefault('display_name', config['expected_model'] or ups_name)
config.setdefault('device_ip', '')
config.setdefault('poll_interval', 15)
if not existing or reset:
    generated_password = secrets.token_urlsafe(24)
    salt = secrets.token_hex(32)
    config.update(username='admin', password_salt=salt, password_iterations=600000,
                  password_hash=hashlib.pbkdf2_hmac('sha256', generated_password.encode(), bytes.fromhex(salt), 600000).hex())
    password_notice.write_text(generated_password + '\n')
    password_notice.chmod(0o600)
if rotate:
    config['nut_password'] = secrets.token_hex(32)

def owned_state_file(filename):
    target = state / filename
    try:
        details = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode) or details.st_uid not in (0, uid):
        raise SystemExit('State file is not recognized or not owned by APCc: ' + str(target))
    return target

settings_path = owned_state_file('settings.json') if rotate else None
settings = json.loads(settings_path.read_text()) if settings_path else None
change_settings = settings is not None and (
    settings.get('nut_host', config['nut_host']) in ('127.0.0.1', 'localhost', '::1')
    and settings.get('nut_port', config['nut_port']) == 3493
    and settings.get('nut_username', config['nut_username']) == 'apcc')
if change_settings:
    settings['nut_password'] = config['nut_password']
auth_path = owned_state_file('auth.json') if reset else None

def atomic_json(target, data, owner, mode):
    descriptor, temporary_name = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, owner, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
if not existing or json.loads(path.read_text()) != config:
    atomic_json(path, config, 0, 0o640)
if change_settings:
    atomic_json(settings_path, settings, uid, 0o600)
if auth_path:
    auth_path.unlink()
if reset:
    # Remove only recognized installer backups containing revoked password
    # hashes. Preserve administrator backups and all NUT backups.
    for backup in path.parent.glob('config.json.backup-*'):
        details = backup.lstat()
        if (re.fullmatch(r'config\.json\.backup-\d{8}T\d{6}Z-\d+', backup.name)
                and stat.S_ISREG(details.st_mode) and details.st_uid == 0):
            backup.unlink()
os.chown(path, 0, gid)
os.chmod(path, 0o640)
PY

python3 - "$CONFIG" "$NUT_USERS" "$STAMP" <<'PY'
import json, os, pathlib, re, shutil, sys
config = json.loads(pathlib.Path(sys.argv[1]).read_text())
path = pathlib.Path(sys.argv[2])
if path.is_symlink():
    raise SystemExit('upsd.users is a symbolic link: automatic modification refused.')
text = path.read_text()
start = '# BEGIN APCC MANAGED USER'
end = '# END APCC MANAGED USER'
if text.count(start) != text.count(end) or text.count(start) > 1:
    raise SystemExit('Inconsistent APCc block markers in upsd.users.')
pattern = re.compile(r'^' + re.escape(start) + r'\n.*?^' + re.escape(end) + r'\n?', re.M | re.S)
outside = pattern.sub('', text)
if start in outside or end in outside:
    raise SystemExit('APCc block in upsd.users is not recognized.')
if re.search(r'^\s*\[\s*apcc\s*\]', outside, re.M):
    raise SystemExit('User apcc is already defined outside the managed block: file not modified.')
commands = ('beeper.disable', 'beeper.enable', 'beeper.mute', 'beeper.off', 'beeper.on', 'test.panel.start',
            'load.off', 'load.off.delay', 'shutdown.reboot', 'shutdown.stop')
block = '\n'.join([start, '[apcc]', '    password = ' + config['nut_password'],
                   '    actions = SET'] + ['    instcmds = ' + command for command in commands] + [end, ''])
new = pattern.sub(lambda _: block, text) if start in text else text.rstrip() + '\n\n' + block
if new != text:
    backup = path.with_name(path.name + '.backup-apcc-' + sys.argv[3])
    shutil.copyfile(path, backup)
    os.chmod(backup, 0o600)
    os.chown(backup, 0, 0)
    stat = path.stat()
    temporary = path.with_name('.upsd.users.apcc-new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(new)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.chmod(temporary, stat.st_mode & 0o660)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
PY
upsd -c reload

[[ -z $(find "$SOURCE_DIR/static" -type l -print -quit) ]] || die 'static/ contains symbolic links: use the original package files.'
STAGE=$(mktemp -d /opt/.apcc.XXXXXXXX)
install -m 0644 "$SOURCE_DIR/app.py" "$STAGE/app.py"
cp -R -- "$SOURCE_DIR/static" "$STAGE/static"
python3 -m py_compile "$STAGE/app.py"
chown -R root:root "$STAGE"
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
  [[ -d "$APP_DIR" && ! -L "$APP_DIR" ]] || die "Unexpected path: $APP_DIR"
  mv -- "$APP_DIR" "$APP_DIR.backup-$STAMP"
fi
mv -- "$STAGE" "$APP_DIR"

cat >"$WORK/apcc.service" <<'UNIT'
[Unit]
Description=APCc - APC Control
After=network.target nut-server.service
Wants=nut-server.service

[Service]
Type=simple
User=apcc
Group=apcc
WorkingDirectory=/opt/apcc
ExecStart=/usr/bin/python3 /opt/apcc/app.py --config /etc/apcc/config.json
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectKernelLogs=yes
ProtectClock=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/var/lib/apcc

[Install]
WantedBy=multi-user.target
UNIT
install -o root -g root -m 0644 "$WORK/apcc.service" /etc/systemd/system/apcc.service

HOST_LABEL=$(python3 - <<'PY'
import re, socket
name = socket.gethostname()
print(name if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', name) else 'localhost')
PY
)
if [[ ! -f "$CONFIG_DIR/tls.crt" || ! -f "$CONFIG_DIR/tls.key" ]]; then
  [[ ! -e "$CONFIG_DIR/tls.crt" && ! -e "$CONFIG_DIR/tls.key" ]] || die 'Incomplete TLS certificate and key pair: restore it before retrying.'
  python3 - <<'PY' >"$WORK/san.txt"
import ipaddress, re, socket, subprocess
names = {'localhost', socket.gethostname(), socket.getfqdn()}
names = sorted(name for name in names if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', name))
addresses = {'127.0.0.1'}
try:
    output = subprocess.check_output(['hostname', '-I'], text=True)
    for token in output.split():
        address = ipaddress.ip_address(token)
        if address.version == 4:
            addresses.add(str(address))
except (OSError, ValueError, subprocess.CalledProcessError):
    pass
print(','.join(['DNS:' + name for name in names] + ['IP:' + value for value in sorted(addresses)]))
PY
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
    -keyout "$CONFIG_DIR/tls.key" -out "$CONFIG_DIR/tls.crt" \
    -subj "/CN=$HOST_LABEL" -addext "subjectAltName=$(cat "$WORK/san.txt")"
fi
chown root:root "$CONFIG_DIR/tls.key" "$CONFIG_DIR/tls.crt"
chmod 0600 "$CONFIG_DIR/tls.key"
chmod 0644 "$CONFIG_DIR/tls.crt"
openssl x509 -in "$CONFIG_DIR/tls.crt" -noout -checkend 0 >/dev/null || die 'The TLS certificate has expired: replace it before proceeding.'

cat >"$WORK/listener.conf" <<'APACHE'
# Managed by apcc installer
Listen 8443 https
APACHE
cat >"$WORK/site.conf" <<APACHE
# Managed by apcc installer
<VirtualHost *:8443>
    ServerName $HOST_LABEL
    SSLEngine on
    SSLCertificateFile /etc/apcc/tls.crt
    SSLCertificateKeyFile /etc/apcc/tls.key
    SSLProtocol all -SSLv3 -TLSv1 -TLSv1.1
    ProxyRequests Off
    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    RequestHeader set X-Panel-Client-IP "expr=%{REMOTE_ADDR}"
    ProxyPass / http://127.0.0.1:8765/ connectiontimeout=5 timeout=30
    ProxyPassReverse / http://127.0.0.1:8765/
    <Location />
        Require ip $ALLOWED
    </Location>
    Header always set X-Content-Type-Options "nosniff"
    Header always set Referrer-Policy "same-origin"
    ErrorLog \${APACHE_LOG_DIR}/apcc-error.log
    CustomLog \${APACHE_LOG_DIR}/apcc-access.log combined
</VirtualHost>

APACHE

site_existed=0; listener_existed=0; site_enabled=0; listener_enabled=0
if [[ -f "$SITE" ]]; then site_existed=1; cp -p -- "$SITE" "$WORK/old-site"; cp -p -- "$SITE" "$SITE.backup-$STAMP"; fi
if [[ -f "$LISTENER" ]]; then listener_existed=1; cp -p -- "$LISTENER" "$WORK/old-listener"; cp -p -- "$LISTENER" "$LISTENER.backup-$STAMP"; fi
[[ ! -L "$SITE_LINK" ]] || site_enabled=1
[[ ! -L "$LISTENER_LINK" ]] || listener_enabled=1
chmod 0600 "$SITE.backup-$STAMP" "$LISTENER.backup-$STAMP" 2>/dev/null || true
install -o root -g root -m 0644 "$WORK/site.conf" "$SITE"
install -o root -g root -m 0644 "$WORK/listener.conf" "$LISTENER"
a2enmod ssl proxy proxy_http headers >/dev/null
a2enconf apcc-listen >/dev/null
a2ensite apcc >/dev/null
if ! apache2ctl configtest; then
  if ((site_existed)); then cp -p -- "$WORK/old-site" "$SITE"; else rm -f -- "$SITE"; fi
  if ((listener_existed)); then cp -p -- "$WORK/old-listener" "$LISTENER"; else rm -f -- "$LISTENER"; fi
  if ((!site_enabled)); then rm -f -- "$SITE_LINK"; fi
  if ((!listener_enabled)); then rm -f -- "$LISTENER_LINK"; fi
  die 'Apache validation failed: APCc files and links were restored. Apache was not reloaded.'
fi
systemctl daemon-reload
systemctl enable apcc.service >/dev/null
systemctl restart apcc.service
python3 - <<'PY'
import socket, time
for attempt in range(30):
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=1):
            break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit('Backend did not start. Check journalctl -u apcc -n 40. Apache was not reloaded.')
PY
systemctl is-active --quiet apcc.service
if systemctl is-active --quiet apache2; then
  systemctl reload apache2
else
  systemctl start apache2
fi
printf '\nAPCc installed: https://%s:8443/ (local access by default)\n' "$HOST_LABEL"
if [[ -f "$WORK/admin-password" ]]; then
  printf 'Username: admin\nGenerated administrator password (shown once): '
  cat -- "$WORK/admin-password"
  rm -f -- "$WORK/admin-password"
  printf 'Store this password securely. It will not be shown again.\n'
else
  printf 'Existing administrator credentials were preserved.\n'
fi
printf 'Allowed networks: %s\n' "$ALLOWED"
printf 'Compare this SHA-256 certificate fingerprint with the browser:\n'
openssl x509 -in "$CONFIG_DIR/tls.crt" -noout -fingerprint -sha256
printf 'No UPS commands were sent. upsmon and the NUT driver were not modified or restarted.\n'
