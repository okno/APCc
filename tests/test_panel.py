"""Isolated security/API checks. Never contact or control an actual UPS.

Run with: python3 -m unittest discover -s tests -v
"""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import socketserver
import shlex
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid


MODULE_PATH = Path(__file__).resolve().parents[1] / 'app.py'
spec = importlib.util.spec_from_file_location('apcc_app', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class ObservedRLock:
    """Signal a chosen contender's first lock attempt without timing sleeps."""
    def __init__(self, watch):
        self.lock = threading.RLock()
        self.watch = watch
        self.attempted = threading.Event()
        self.was_blocked = None

    def __enter__(self):
        if self.watch(threading.current_thread()) and not self.attempted.is_set():
            acquired = self.lock.acquire(blocking=False)
            self.was_blocked = not acquired
            self.attempted.set()
            if acquired:
                return self
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class MockNut:
    def __init__(self):
        self.commands = []
        self.command_extras = []
        self.settings = []
        self.authentications = 0
        self.supported_commands = [
            'beeper.disable', 'beeper.enable', 'beeper.mute', 'beeper.off',
            'beeper.on', 'load.off', 'load.off.delay', 'shutdown.reboot',
            'shutdown.stop',
        ]
        self.supported_variables = [
            'battery.charge.low', 'battery.runtime.low', 'ups.delay.shutdown'
        ]
        self.variables = {
            'device.model': 'Demo UPS',
            'device.serial': 'DEMO-UPS-0001',
            'ups.model': 'Demo UPS',
            'ups.serial': 'DEMO-UPS-0001',
            'ups.status': 'OL',
            'battery.charge': '97',
            'battery.charge.low': '10',
            'battery.runtime.low': '150',
            'ups.delay.shutdown': '20',
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _authenticate(self):
        self.authentications += 1

    def snapshot(self):
        return {
            'variables': dict(self.variables),
            'supported_commands': list(self.supported_commands),
            'supported_variables': list(self.supported_variables),
        }

    def command(self, command, extra=None):
        self.commands.append(command)
        self.command_extras.append((command, extra))

    def set_variable(self, variable, value):
        self.settings.append((variable, value))
        self.variables[variable] = str(value)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.password = 'correct-test-password'
        salt = b'isolated-test-salt-2026'
        cls.audit_path = Path(cls.temporary.name) / 'audit.jsonl'
        cls.config = {
            'username': 'admin',
            'password_salt': salt.hex(),
            'password_hash': hashlib.pbkdf2_hmac(
                'sha256', cls.password.encode(), salt, 10000
            ).hex(),
            'password_iterations': 10000,
            'audit_path': str(cls.audit_path),
            'nut_host': '127.0.0.1',
            'nut_port': 3493,
            'ups': 'apc',
            'nut_username': 'nut-admin-test',
            'nut_password': 'never-log-this-nut-password',
            'expected_model': 'Demo UPS',
            'expected_serial': 'DEMO-UPS-0001',
        }
        cls.application = module.Application(cls.config)
        cls.server = module.ThreadingHTTPServer(
            ('127.0.0.1', 0), module.make_handler(cls.application)
        )
        cls.port = cls.server.server_address[1]
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)
        cls.temporary.cleanup()

    def setUp(self):
        self.nut = MockNut()
        self.application.nut_factory = lambda: self.nut
        self.cookie = None
        self.csrf = None

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        request_headers = {
            'X-Forwarded-Proto': 'https',
            'Origin': f'https://127.0.0.1:{self.port}',
        }
        if self.cookie:
            request_headers['Cookie'] = self.cookie
        if self.csrf:
            request_headers['X-CSRF-Token'] = self.csrf
        if data is not None:
            request_headers['Content-Type'] = 'application/json'
        if headers:
            for key, value in headers.items():
                if value is None:
                    request_headers.pop(key, None)
                else:
                    request_headers[key] = value
        connection.request(
            method, path,
            body=json.dumps(data).encode() if data is not None else None,
            headers=request_headers,
        )
        response = connection.getresponse()
        status = response.status
        response_headers = dict(response.getheaders())
        body = response.read()
        connection.close()
        try:
            body = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            pass
        return status, response_headers, body

    def login(self):
        status, headers, body = self.request('POST', '/api/login', {
            'username': 'admin', 'password': self.password,
        })
        self.assertEqual(status, 200, body)
        self.assertIn('Set-Cookie', headers)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        status, _, session = self.request('GET', '/api/session')
        self.assertEqual(status, 200, session)
        self.assertTrue(session['authenticated'])
        self.csrf = session['csrf']
        self.assertTrue(self.csrf)
        return headers

    @staticmethod
    def action(command='beeper.mute', **extra):
        return {'command': command, 'request_id': str(uuid.uuid4()), **extra}

    def assert_rejected(self, result):
        self.assertIn(result[0], (400, 401, 403, 404, 405, 409, 422, 429), result)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_authentication_required_for_status_and_action(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)
        self.assert_rejected(self.request('POST', '/api/action', self.action()))

    def test_login_cookie_security_flags(self):
        headers = self.login()
        cookie = headers['Set-Cookie'].lower()
        for expected in ('secure', 'httponly', 'samesite=strict'):
            self.assertIn(expected, cookie)

    def test_invalid_login_never_returns_session_credentials(self):
        status, headers, body = self.request('POST', '/api/login', {
            'username': 'admin', 'password': 'wrong-password',
        })
        self.assertEqual(status, 401, body)
        self.assertNotIn('Set-Cookie', headers)
        serialized = json.dumps(body)
        self.assertNotIn(self.config['password_hash'], serialized)
        self.assertNotIn(self.config['nut_password'], serialized)

    def test_login_requires_https_origin_matching_host(self):
        self.assert_rejected(self.request('POST', '/api/login', {
            'username': 'admin', 'password': self.password,
        }, {'Origin': 'https://attacker.invalid'}))

    def test_action_requires_csrf_token(self):
        self.login()
        self.assert_rejected(self.request(
            'POST', '/api/action', self.action(), {'X-CSRF-Token': None}
        ))

    def test_action_rejects_wrong_csrf_token(self):
        self.login()
        self.assert_rejected(self.request(
            'POST', '/api/action', self.action(), {'X-CSRF-Token': 'invalid'}
        ))

    def test_action_requires_matching_origin(self):
        self.login()
        self.assert_rejected(self.request(
            'POST', '/api/action', self.action(), {'Origin': 'https://attacker.invalid'}
        ))

    def test_action_rejects_missing_origin(self):
        self.login()
        self.assert_rejected(self.request(
            'POST', '/api/action', self.action(), {'Origin': None}
        ))

    def test_commands_cannot_escape_allowlist(self):
        self.login()
        for command in ('beeper.mute;id', 'beeper.mute\nINSTCMD apc load.off',
                        'shutdown.return', 'load.on', '', ['load.off']):
            with self.subTest(command=command):
                self.assert_rejected(self.request(
                    'POST', '/api/action', self.action(command)
                ))

    def test_destructive_commands_require_exact_confirmation(self):
        self.login()
        for command in ('load.off', 'load.off.delay', 'shutdown.reboot'):
            for confirmation in ('', 'yes', 'power off', 'POWER OFF '):
                with self.subTest(command=command, confirmation=confirmation):
                    self.assert_rejected(self.request(
                        'POST', '/api/action', self.action(command, confirmation=confirmation)
                    ))

    def test_destructive_command_with_confirmation_is_sent_once(self):
        self.login()
        result = self.request('POST', '/api/action', self.action(
            'load.off', confirmation='POWER OFF'
        ))
        self.assertEqual(result[0], 200, result)
        self.assertEqual(self.nut.commands, ['load.off'])

    def test_unsupported_command_is_not_sent_to_nut(self):
        self.login()
        self.nut.supported_commands.remove('beeper.mute')
        self.assert_rejected(self.request('POST', '/api/action', self.action()))

    def test_readonly_requests_cannot_control_ups(self):
        self.login()
        for path in ('/api/status', '/api/session', '/api/action?command=load.off',
                     '/api/setting?variable=battery.charge.low&value=99'):
            self.request('GET', path)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_parameter_validation_rejects_nonintegers_and_ranges(self):
        self.login()
        for variable, value in (
            ('battery.charge.low', '-1'), ('battery.charge.low', '101'),
            ('battery.charge.low', '1.5'), ('battery.charge.low', 'NaN'),
            ('battery.charge.low', '10\nINSTCMD apc load.off'),
            ('battery.runtime.low', '-1'), ('ups.delay.shutdown', '-1'),
            ('unsupported.variable', '10'), ('battery.charge.low', True),
        ):
            with self.subTest(variable=variable, value=value):
                self.assert_rejected(self.request('POST', '/api/setting', {
                    'variable': variable, 'value': value, 'request_id': str(uuid.uuid4()),
                }))

    def test_unsupported_variable_is_not_written(self):
        self.login()
        self.nut.supported_variables.remove('battery.charge.low')
        self.assert_rejected(self.request('POST', '/api/setting', {
            'variable': 'battery.charge.low', 'value': '15', 'request_id': str(uuid.uuid4()),
        }))

    def test_valid_parameter_is_written(self):
        self.login()
        result = self.request('POST', '/api/setting', {
            'variable': 'battery.charge.low', 'value': '15', 'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(result[0], 200, result)
        self.assertEqual([(k, str(v)) for k, v in self.nut.settings], [('battery.charge.low', '15')])

    def test_duplicate_request_id_never_executes_twice(self):
        self.login()
        action = self.action()
        first = self.request('POST', '/api/action', action)
        self.assertEqual(first[0], 200, first)
        second = self.request('POST', '/api/action', action)
        self.assertIn(second[0], (200, 409), second)
        self.assertEqual(self.nut.commands, ['beeper.mute'])

    def test_request_id_reuse_for_different_command_is_rejected(self):
        self.login()
        action = self.action()
        self.assertEqual(self.request('POST', '/api/action', action)[0], 200)
        action['command'] = 'beeper.enable'
        result = self.request('POST', '/api/action', action)
        self.assertIn(result[0], (400, 409), result)
        self.assertEqual(self.nut.commands, ['beeper.mute'])

    def test_usb_serial_padding_does_not_block_valid_identity(self):
        self.login()
        self.nut.variables['device.serial'] = 'DEMO-UPS-0001    '
        self.nut.variables['ups.serial'] = 'DEMO-UPS-0001    '
        result = self.request('POST', '/api/action', self.action())
        self.assertEqual(result[0], 200, result)
        self.assertEqual(self.nut.commands, ['beeper.mute'])

    def test_wrong_ups_identity_prevents_commands(self):
        self.login()
        self.nut.variables['device.serial'] = 'DIFFERENT-UPS'
        self.nut.variables['ups.serial'] = 'DIFFERENT-UPS'
        self.assert_rejected(self.request('POST', '/api/action', self.action()))

    def test_audit_file_persists_without_secrets(self):
        self.login()
        result = self.request('POST', '/api/action', self.action())
        self.assertEqual(result[0], 200, result)
        self.assertTrue(self.audit_path.exists())
        audit = self.audit_path.read_text()
        self.assertIn('beeper.mute', audit)
        self.assertIn('admin', audit)
        for secret in (self.password, self.config['nut_password'],
                       self.config['password_hash'], self.csrf, self.cookie):
            self.assertNotIn(secret, audit)
        for line in audit.splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_post_dispatch_audit_failure_never_claims_not_sent(self):
        self.login()
        original_audit = self.application.audit

        def failing_audit(actor, action, target, result):
            if result != 'dispatching':
                raise OSError('simulated audit disk full')
            return original_audit(actor, action, target, result)

        self.application.audit = failing_audit
        action = self.action()
        try:
            with self.assertLogs(level='ERROR'):
                result = self.request('POST', '/api/action', action)
        finally:
            self.application.audit = original_audit
        self.assertIn(result[0], (200, 500, 502, 503), result)
        message = json.dumps(result[2], ensure_ascii=False).lower()
        self.assertNotIn('not sent', message)
        self.assertEqual(self.nut.commands, ['beeper.mute'])
        self.request('POST', '/api/action', action)
        self.assertEqual(self.nut.commands, ['beeper.mute'])

    def test_connection_timeout_after_dispatch_cannot_replay(self):
        self.login()

        def uncertain_command(command):
            self.nut.commands.append(command)
            raise TimeoutError('simulated reply timeout')

        self.nut.command = uncertain_command
        action = self.action()
        result = self.request('POST', '/api/action', action)
        self.assertEqual(result[0], 502, result)
        self.assertIn('uncertain', result[2]['error'])
        repeated = self.request('POST', '/api/action', action)
        self.assertIn(repeated[0], (409, 502), repeated)
        self.assertEqual(self.nut.commands, ['beeper.mute'])

    def test_deduplication_survives_application_restart(self):
        self.login()
        action = self.action()
        result = self.request('POST', '/api/action', action)
        self.assertEqual(result[0], 200, result)
        restarted = module.Application(self.config)
        after_restart = MockNut()
        restarted.nut_factory = lambda: after_restart
        status, response = restarted.mutate('admin', 'command', action)
        self.assertEqual(status, 200, response)
        self.assertEqual(after_restart.commands, [])
        self.assertEqual(self.nut.commands, ['beeper.mute'])

    def test_status_does_not_disclose_credentials(self):
        self.login()
        result = self.request('GET', '/api/status')
        self.assertEqual(result[0], 200, result)
        serialized = json.dumps(result[2])
        for secret in (self.password, self.config['nut_password'], self.config['password_hash']):
            self.assertNotIn(secret, serialized)


class ConfigurationApiTests(unittest.TestCase):
    # Reuse the HTTP harness while giving every configuration test fresh storage.
    request = ApiTests.request
    login = ApiTests.login
    action = staticmethod(ApiTests.action)
    assert_rejected = ApiTests.assert_rejected

    def setUp(self):
        ApiTests.setUpClass.__func__(type(self))
        ApiTests.setUp(self)
        self.config_probes = []

        def probe(candidate):
            self.config_probes.append(dict(candidate))
            return self.nut

        self.application.config_nut_factory = probe

    def tearDown(self):
        ApiTests.tearDownClass.__func__(type(self))

    def test_new_endpoints_require_authentication_and_csrf(self):
        self.assertEqual(self.request('GET', '/api/config')[0], 401)
        routes = ('/api/config', '/api/config/test', '/api/password',
                  '/api/outlets', '/api/beeper-test')
        for route in routes:
            with self.subTest(route=route, authenticated=False):
                self.assertEqual(self.request('POST', route, {})[0], 401)
        self.login()
        for route in routes:
            with self.subTest(route=route, authenticated=True):
                self.assertEqual(self.request('POST', route, {},
                                             {'X-CSRF-Token': None})[0], 403)
        self.assertEqual(self.config_probes, [])
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_public_configuration_omits_credentials(self):
        self.login()
        status, _, config = self.request('GET', '/api/config')
        self.assertEqual(status, 200, config)
        self.assertTrue(config['has_nut_password'])
        for key in ('nut_password', 'password_hash', 'password_salt'):
            self.assertNotIn(key, config)
        serialized = json.dumps(config)
        self.assertNotIn(self.config['nut_password'], serialized)
        self.assertNotIn(self.config['password_hash'], serialized)

    def test_connection_test_is_readonly_and_rejects_unsafe_hosts(self):
        self.login()
        for host in ('203.0.113.10', '169.254.169.254', '0.0.0.0', '224.0.0.1', 'example.com'):
            with self.subTest(host=host):
                result = self.request('POST', '/api/config/test', {'nut_host': host})
                self.assertEqual(result[0], 400, result)
        self.assertEqual(self.config_probes, [])
        result = self.request('POST', '/api/config/test', {'nut_host': '127.0.0.1'})
        self.assertEqual(result[0], 200, result)
        self.assertEqual(self.nut.authentications, 0)
        self.assertIs(result[2]['credentials_verified'], False)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])
        self.assertFalse(self.application.settings_path.exists())

    def test_nut_password_whitespace_is_preserved_exactly(self):
        self.login()
        password = ' secret with significant edge spaces '
        result = self.request('POST', '/api/config/test', {'nut_password': password})
        self.assertEqual(result[0], 200, result)
        self.assertEqual(self.config_probes[-1]['nut_password'], password)
        self.assertEqual(self.nut.authentications, 0)
        self.assertIs(result[2]['credentials_verified'], False)
        self.assertNotIn(password, json.dumps(result[2]))
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_configuration_persists_and_blank_password_preserves_secret(self):
        self.login()
        result = self.request('POST', '/api/config', {
            'display_name': 'Demo lab UPS', 'poll_interval': 20,
            'nut_password': '', 'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(result[0], 200, result)
        self.assertEqual(result[2]['config']['display_name'], 'Demo lab UPS')
        self.assertNotIn(self.config['nut_password'], json.dumps(result[2]))
        stored = json.loads(self.application.settings_path.read_text())
        self.assertEqual(stored['nut_password'], self.config['nut_password'])
        restarted = module.Application(self.config)
        self.assertEqual(restarted.config_public()['display_name'], 'Demo lab UPS')
        self.assertEqual(restarted.config_public()['poll_interval'], 20)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_connection_save_rejects_wrong_serial_without_persisting(self):
        self.login()
        result = self.request('POST', '/api/config', {
            'nut_host': '192.168.1.20', 'expected_serial': 'WRONG-SERIAL',
            'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(result[0], 409, result)
        self.assertFalse(self.application.settings_path.exists())
        self.assertEqual(self.application.config['nut_host'], '127.0.0.1')
        self.assertEqual(self.nut.authentications, 0)
        self.assertEqual(self.nut.commands, [])

    def test_password_change_hashes_secret_and_revokes_sessions(self):
        self.login()
        new_password = 'new-isolated-test-password'
        wrong = self.request('POST', '/api/password', {
            'current_password': 'wrong', 'new_password': new_password,
            'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(wrong[0], 403, wrong)
        self.assertFalse(self.application.auth_path.exists())
        result = self.request('POST', '/api/password', {
            'current_password': self.password, 'new_password': new_password,
            'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(result[0], 200, result)
        self.assertEqual(self.request('GET', '/api/status')[0], 401)
        stored_text = self.application.auth_path.read_text()
        self.assertNotIn(new_password, stored_text)
        auth = json.loads(stored_text)
        expected = hashlib.pbkdf2_hmac('sha256', new_password.encode(),
                                       bytes.fromhex(auth['password_salt']),
                                       auth['password_iterations']).hex()
        self.assertEqual(auth['password_hash'], expected)
        self.assertNotEqual(auth['password_hash'], self.config['password_hash'])
        result = self.request('POST', '/api/login', {
            'username': 'admin', 'password': new_password,
        })
        self.assertEqual(result[0], 200, result)
        self.assertNotIn(new_password, self.audit_path.read_text())

    def test_password_reset_revokes_login_that_was_checking_old_password(self):
        checked_old_hash = threading.Event()
        continue_login = threading.Event()
        observed = ObservedRLock(lambda thread: thread.name == 'password-reset-race')
        self.application.session_lock = observed
        original_compare = module.hmac.compare_digest
        results, errors = {}, []

        def pause_after_password_compare(left, right):
            valid = original_compare(left, right)
            if (threading.current_thread().name == 'old-password-login-race'
                    and isinstance(left, str) and right == self.config['password_hash']):
                checked_old_hash.set()
                if not continue_login.wait(3):
                    raise RuntimeError('test login barrier timed out')
            return valid

        def login_worker():
            try:
                results['login'] = self.application.authenticate('admin', self.password, 'race-client')
            except BaseException as error:
                errors.append(error)

        def reset_worker():
            try:
                results['reset'] = self.application.change_password('admin', {
                    'current_password': self.password,
                    'new_password': 'reset-during-login-password',
                    'request_id': str(uuid.uuid4()),
                })
            except BaseException as error:
                errors.append(error)

        login_thread = threading.Thread(target=login_worker, name='old-password-login-race')
        reset_thread = threading.Thread(target=reset_worker, name='password-reset-race')
        with mock.patch.object(module.hmac, 'compare_digest', side_effect=pause_after_password_compare):
            login_thread.start()
            try:
                self.assertTrue(checked_old_hash.wait(3), 'login did not reach password check')
                reset_thread.start()
                self.assertTrue(observed.attempted.wait(3), 'password reset did not reach session lock')
                self.assertTrue(observed.was_blocked,
                                'password reset raced between validation and session creation')
            finally:
                continue_login.set()
                login_thread.join(timeout=4)
                if reset_thread.ident is not None:
                    reset_thread.join(timeout=4)
        self.assertFalse(login_thread.is_alive())
        self.assertFalse(reset_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIn('reset', results)
        token = results['login'][0]
        self.assertIsNone(self.application.session(token),
                          'a session using the old password survived password reset')

    def test_queued_command_rechecks_session_after_password_reset(self):
        self.login()
        main_thread = threading.current_thread()
        observed = ObservedRLock(lambda thread: thread is not main_thread)
        self.application.operation_lock = observed
        results, errors = [], []

        def queued_request():
            try:
                results.append(self.request('POST', '/api/action', self.action()))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=queued_request, name='queued-command-client')
        with observed:
            worker.start()
            try:
                self.assertTrue(observed.attempted.wait(2), 'HTTP request did not reach operation lock')
                self.assertTrue(observed.was_blocked, 'HTTP request was not queued')
                self.application.change_password('admin', {
                    'current_password': self.password,
                    'new_password': 'reset-before-queued-command',
                    'request_id': str(uuid.uuid4()),
                })
            finally:
                # Exiting this block releases the queued HTTP handler.
                pass
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 401, results)
        self.assertEqual(self.nut.commands, [],
                         'an old session dispatched a queued hardware command')
        self.assertEqual(self.nut.settings, [])

    def test_beeper_test_unavailable_does_not_enable_beeper_as_substitute(self):
        self.login()
        result = self.request('POST', '/api/beeper-test', {'request_id': str(uuid.uuid4())})
        self.assertEqual(result[0], 409, result)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_beeper_test_uses_only_reported_panel_test_once(self):
        self.login()
        self.nut.supported_commands.append('test.panel.start')
        action = {'request_id': str(uuid.uuid4())}
        result = self.request('POST', '/api/beeper-test', action)
        self.assertEqual(result[0], 200, result)
        repeated = self.request('POST', '/api/beeper-test', action)
        self.assertEqual(repeated[0], 200, repeated)
        self.assertEqual(self.nut.commands, ['test.panel.start'])

    def test_delayed_shutdown_uses_live_configured_delay(self):
        self.login()
        self.nut.variables['ups.delay.shutdown'] = '47'
        result = self.request('POST', '/api/action', self.action(
            'load.off.delay', confirmation='POWER OFF'
        ))
        self.assertEqual(result[0], 200, result)
        self.assertEqual(self.nut.command_extras, [('load.off.delay', '47')])

    def test_outlet_estimates_are_saved_without_claiming_metered_watts(self):
        self.login()
        outlets = json.loads(json.dumps(self.application.config_public()['outlets']))
        outlets[0].update(device='Demo server', estimated_w=120.5)
        outlets[1].update(device='Switch', estimated_w=50)
        result = self.request('POST', '/api/outlets', {
            'outlets': outlets, 'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(result[0], 200, result)
        status = self.request('GET', '/api/status')[2]['consumption']
        self.assertEqual(status['estimated_total_w'], 170.5)
        self.assertEqual(status['estimated_count'], 2)
        self.assertIsNone(status['measured_total_w'])
        self.assertFalse(status['per_outlet_metering'])
        self.nut.variables.update({'ups.realpower': '190', 'ups.power': '210', 'ups.load': '8'})
        status = self.request('GET', '/api/status')[2]['consumption']
        self.assertEqual(status['measured_total_w'], 190)
        self.assertEqual(status['estimated_total_w'], 170.5)
        self.assertEqual(status['apparent_power_va'], 210)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])

    def test_outlet_validation_rejects_duplicate_ids_and_invalid_estimates(self):
        self.login()
        template = self.application.config_public()['outlets']
        for value in (-1, 100001, 'NaN', 'Infinity', True):
            entries = json.loads(json.dumps(template))
            entries[0]['estimated_w'] = value
            with self.subTest(value=value):
                result = self.request('POST', '/api/outlets', {
                    'outlets': entries, 'request_id': str(uuid.uuid4()),
                })
                self.assertEqual(result[0], 400, result)
        entries = json.loads(json.dumps(template))
        entries[1]['id'] = entries[0]['id']
        result = self.request('POST', '/api/outlets', {
            'outlets': entries, 'request_id': str(uuid.uuid4()),
        })
        self.assertEqual(result[0], 400, result)
        self.assertEqual(self.nut.commands, [])
        self.assertEqual(self.nut.settings, [])


class FakeNutHandler(socketserver.StreamRequestHandler):
    """Minimal local NUT daemon, recording every request for assertions."""
    def handle(self):
        while True:
            raw = self.rfile.readline(16384)
            if not raw:
                return
            line = raw.decode('utf-8').rstrip('\r\n')
            self.server.requests.append(line)
            try:
                words = shlex.split(line)
            except ValueError:
                self.reply('ERR INVALID-ARGUMENT')
                continue
            if not words:
                self.reply('ERR INVALID-ARGUMENT')
            elif words[0] in ('USERNAME', 'PASSWORD', 'LOGIN', 'LOGOUT'):
                self.reply('OK')
                if words[0] == 'LOGOUT':
                    return
            elif words[:2] == ['LIST', 'VAR']:
                self.reply('BEGIN LIST VAR apc')
                for name, value in self.server.variables.items():
                    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
                    self.reply(f'VAR apc {name} "{escaped}"')
                self.reply('END LIST VAR apc')
            elif words[:2] == ['LIST', 'CMD']:
                self.reply('BEGIN LIST CMD apc')
                self.reply('CMD apc beeper.mute')
                self.reply('CMD apc load.off')
                self.reply('END LIST CMD apc')
            elif words[:2] == ['LIST', 'RW']:
                self.reply('BEGIN LIST RW apc')
                self.reply('RW apc battery.charge.low "10"')
                self.reply('END LIST RW apc')
            elif words[:2] == ['GET', 'VAR'] and len(words) == 4:
                value = self.server.variables.get(words[3])
                if value is None:
                    self.reply('ERR VAR-NOT-SUPPORTED')
                else:
                    self.reply(f'VAR apc {words[3]} "{value}"')
            elif words[0] == 'INSTCMD':
                self.reply('ERR ACCESS-DENIED' if self.server.deny_commands else 'OK')
            elif words[:2] == ['SET', 'VAR']:
                self.reply('OK')
            else:
                self.reply('ERR UNKNOWN-COMMAND')

    def reply(self, text):
        self.wfile.write((text + '\n').encode('utf-8'))
        self.wfile.flush()


class FakeNutServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class NutProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeNutServer(('127.0.0.1', 0), FakeNutHandler)
        self.server.requests = []
        self.server.variables = {
            'device.model': 'Demo UPS',
            'device.serial': 'DEMO-UPS-0001',
            'ups.status': 'OL',
            'battery.charge.low': '10',
            'device.mfr': 'American Power Conversion',
        }
        self.server.deny_commands = False
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.config = {
            'nut_host': '127.0.0.1',
            'nut_port': self.server.server_address[1],
            'ups': 'apc',
            'nut_username': 'nut-admin-test',
            'nut_password': 'a secret with spaces',
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)

    def test_snapshot_parses_quoted_values_and_capabilities(self):
        with module.NutClient(self.config) as nut:
            snapshot = nut.snapshot()
        self.assertEqual(snapshot['variables']['device.mfr'], 'American Power Conversion')
        self.assertIn('beeper.mute', snapshot['supported_commands'])
        self.assertIn('battery.charge.low', snapshot['supported_variables'])
        self.assertFalse(any(line.startswith(('INSTCMD ', 'SET VAR '))
                             for line in self.server.requests))

    def test_command_authenticates_and_sends_single_request(self):
        with module.NutClient(self.config) as nut:
            nut.command('beeper.mute')
        parsed = [shlex.split(line) for line in self.server.requests]
        self.assertIn(['USERNAME', 'nut-admin-test'], parsed)
        self.assertIn(['PASSWORD', 'a secret with spaces'], parsed)
        self.assertEqual([words for words in parsed if words[0] == 'INSTCMD'],
                         [['INSTCMD', 'apc', 'beeper.mute']])

    def test_delay_is_sent_as_a_single_extra_protocol_value(self):
        with module.NutClient(self.config) as nut:
            nut.command('load.off.delay', '45')
        parsed = [shlex.split(line) for line in self.server.requests]
        self.assertEqual([words for words in parsed if words[0] == 'INSTCMD'],
                         [['INSTCMD', 'apc', 'load.off.delay', '45']])

    def test_extra_argument_cannot_inject_nut_protocol_commands(self):
        for extra in ('45\nINSTCMD apc load.off', '45 0', '-1', '1.5', True):
            with self.subTest(extra=extra):
                with module.NutClient(self.config) as nut:
                    with self.assertRaises(ValueError):
                        nut.command('load.off.delay', extra)
        self.assertFalse(any(line.startswith('INSTCMD ')
                             for line in self.server.requests))

    def test_setting_is_written_as_a_single_protocol_value(self):
        with module.NutClient(self.config) as nut:
            nut.set_variable('battery.charge.low', '15')
        parsed = [shlex.split(line) for line in self.server.requests]
        self.assertIn(['SET', 'VAR', 'apc', 'battery.charge.low', '15'], parsed)

    def test_daemon_command_failure_is_not_reported_as_success(self):
        self.server.deny_commands = True
        with module.NutClient(self.config) as nut:
            with self.assertRaises(Exception):
                nut.command('beeper.mute')


if __name__ == '__main__':
    unittest.main()
