import os
import unittest
from unittest.mock import patch, MagicMock

import requests


class RegisterTests(unittest.TestCase):
    """Tests for the register function."""

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    @patch('apps.utils.greffon.base_server._write_local_cert')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_posts_to_base_server(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_write_cert, mock_exists
    ):
        """register() POSTs to the manager with the correct payload, writes
        cert material locally (key before cert), and copies it into the nginx
        container on 200. No cert material exists before this call, so the
        POST runs in the bootstrap verify=True branch (no client cert)."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod._registered.clear()

        # requests is patched as a module, but exception classes used in
        # `except requests.RequestException` still need to be real.
        mock_requests.RequestException = requests.RequestException

        env_values = {
            'GREFFER_ADDRESS': '10.0.0.1',
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values):
            mock_get_response = MagicMock()
            mock_get_response.status_code = 200
            mock_get_response.json.return_value = {
                'certificate': 'CERT_DATA',
                'private_key': 'KEY_DATA',
            }
            mock_requests.get.return_value = mock_get_response

            mod.register()

        mock_requests.post.assert_any_call(
            'https://test.greffon.io/api/greffer/register/greffer-abc/',
            json={
                'address': '10.0.0.1',
                'port': '8443',
                'token': 'fake-token',
                'protocol': 'https',
            },
            timeout=mod.REQUEST_TIMEOUT,
            verify=True,
        )

        # Key must be written before cert (durability invariant).
        calls = [c for c in mock_write_cert.call_args_list if c.args[0] in ('cert.key', 'pem.crt')]
        self.assertEqual([c.args[0] for c in calls], ['cert.key', 'pem.crt'])
        mock_write_cert.assert_any_call('cert.key', 'KEY_DATA', mode=0o600)
        mock_write_cert.assert_any_call('pem.crt', 'CERT_DATA')

        mock_copy_file.assert_any_call('test-nginx', '/root', 'pem.crt', 'CERT_DATA')
        mock_copy_file.assert_any_call('test-nginx', '/root', 'cert.key', 'KEY_DATA')

        self.assertTrue(mod._registered.is_set())

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    @patch('apps.utils.greffon.base_server._write_local_cert')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_uses_hostname_when_no_address_env(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_write_cert, mock_exists
    ):
        """When GREFFER_ADDRESS is not set, register() resolves the local
        hostname and uses its IP address."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod._registered.clear()
        mock_requests.RequestException = requests.RequestException

        mock_socket.gethostname.return_value = 'my-host'
        mock_socket.gethostbyname.return_value = '192.168.1.50'

        env_values = {
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values, clear=False):
            os.environ.pop('GREFFER_ADDRESS', None)

            mock_get_response = MagicMock()
            mock_get_response.status_code = 200
            mock_get_response.json.return_value = {
                'certificate': 'CERT',
                'private_key': 'KEY',
            }
            mock_requests.get.return_value = mock_get_response

            mod.register()

        post_call_kwargs = mock_requests.post.call_args
        self.assertEqual(post_call_kwargs[1]['json']['address'], '192.168.1.50')
        mock_socket.gethostname.assert_called_once()
        mock_socket.gethostbyname.assert_called_once_with('my-host')

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    @patch('apps.utils.greffon.base_server._write_local_cert')
    @patch('apps.utils.greffon.base_server.time')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_retries_cert_fetch(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_time, mock_write_cert, mock_exists
    ):
        """When the certificate endpoint returns a non-200 status, register()
        sleeps and retries until 200."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod._registered.clear()
        mock_requests.RequestException = requests.RequestException

        env_values = {
            'GREFFER_ADDRESS': '10.0.0.1',
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values):
            fail_response = MagicMock()
            fail_response.status_code = 401

            success_response = MagicMock()
            success_response.status_code = 200
            success_response.json.return_value = {
                'certificate': 'CERT',
                'private_key': 'KEY',
            }

            mock_requests.get.side_effect = [fail_response, success_response]

            mod.register()

        mock_time.sleep.assert_called_once_with(5)
        self.assertEqual(mock_requests.get.call_count, 2)

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    @patch('apps.utils.greffon.base_server._write_local_cert')
    @patch('apps.utils.greffon.base_server.time')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_retries_on_network_exception(
        self, mock_socket, mock_get_token, mock_copy_file, mock_time, mock_write_cert, mock_exists
    ):
        """register() catches requests.RequestException on both the POST and
        the GET (not just ConnectionError). A single SSLError must not kill
        the registration thread."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod._registered.clear()

        env_values = {
            'GREFFER_ADDRESS': '10.0.0.1',
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {'certificate': 'CERT', 'private_key': 'KEY'}

        with patch.dict(os.environ, env_values):
            with patch('apps.utils.greffon.base_server.requests') as mock_requests:
                mock_requests.RequestException = requests.RequestException
                mock_requests.post.side_effect = [requests.Timeout(), None]
                mock_requests.get.side_effect = [requests.exceptions.SSLError(), success_response]

                mod.register()

                self.assertEqual(mock_requests.post.call_count, 2)
                self.assertEqual(mock_requests.get.call_count, 2)

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    @patch('apps.utils.greffon.base_server._write_local_cert')
    @patch('apps.utils.greffon.base_server.time')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_retries_on_malformed_cert_response(
        self, mock_socket, mock_get_token, mock_copy_file, mock_time, mock_write_cert, mock_exists
    ):
        """A 200 with missing required fields must not crash the thread — log
        and retry until a well-formed response arrives."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod._registered.clear()

        malformed = MagicMock()
        malformed.status_code = 200
        malformed.json.return_value = {'unexpected': 'payload'}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {'certificate': 'CERT', 'private_key': 'KEY'}

        env_values = {
            'GREFFER_ADDRESS': '10.0.0.1',
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values):
            with patch('apps.utils.greffon.base_server.requests') as mock_requests:
                mock_requests.RequestException = requests.RequestException
                mock_requests.get.side_effect = [malformed, success]

                mod.register()

                self.assertEqual(mock_requests.get.call_count, 2)
                self.assertTrue(mod._registered.is_set())


class CheckSecureBootstrapTests(unittest.TestCase):
    def test_allows_https(self):
        import apps.utils.greffon.base_server as mod

        original = mod.base_server
        try:
            mod.base_server = 'https://api.greffon.io'
            # Should not raise
            mod._check_secure_bootstrap()
        finally:
            mod.base_server = original

    def test_refuses_http_without_opt_in(self):
        import apps.utils.greffon.base_server as mod

        original = mod.base_server
        try:
            mod.base_server = 'http://host.docker.internal:8000'
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('GREFFER_ALLOW_INSECURE_BOOTSTRAP', None)
                with self.assertRaises(RuntimeError) as cm:
                    mod._check_secure_bootstrap()
                self.assertIn('http://host.docker.internal:8000', str(cm.exception))
                self.assertIn('GREFFER_ALLOW_INSECURE_BOOTSTRAP', str(cm.exception))
        finally:
            mod.base_server = original

    def test_http_allowed_with_opt_in(self):
        import apps.utils.greffon.base_server as mod

        original = mod.base_server
        try:
            mod.base_server = 'http://host.docker.internal:8000'
            with patch.dict(os.environ, {'GREFFER_ALLOW_INSECURE_BOOTSTRAP': '1'}):
                mod._check_secure_bootstrap()
        finally:
            mod.base_server = original


class ClientAuthTests(unittest.TestCase):
    """Tests for _client_auth() — the mTLS kwargs selector."""

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    def test_bootstrap_when_cert_files_missing(self, mock_exists):
        """Before registration completes, no cert material is on disk — fall
        back to system-CA verification with no client cert."""
        import apps.utils.greffon.base_server as mod

        auth = mod._client_auth()

        self.assertEqual(auth, {'verify': True})

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=True)
    def test_mtls_when_all_cert_material_present(self, mock_exists):
        """Post-registration with issuing_ca: verify against the manager-issued
        CA and present the greffer's client cert."""
        import apps.utils.greffon.base_server as mod

        auth = mod._client_auth()

        self.assertEqual(
            auth,
            {
                'verify': mod.CA_PATH,
                'cert': (mod.CERT_PATH, mod.KEY_PATH),
            },
        )

    def test_presents_cert_when_ca_missing(self):
        """register() treats issuing_ca as optional. When cert+key exist but
        ca.pem doesn't, still present the client cert (falling back to
        system-CA verification)."""
        import apps.utils.greffon.base_server as mod

        def exists(path):
            return path in (mod.CERT_PATH, mod.KEY_PATH)

        with patch('apps.utils.greffon.base_server.os.path.exists', side_effect=exists):
            auth = mod._client_auth()

        self.assertEqual(
            auth,
            {
                'verify': True,
                'cert': (mod.CERT_PATH, mod.KEY_PATH),
            },
        )


class ChangeStatusTests(unittest.TestCase):
    """Tests for the change_status function."""

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=True)
    @patch('apps.utils.greffon.base_server.requests')
    def test_change_status_posts_with_mtls(self, mock_requests, mock_exists):
        """change_status runs after registration, so cert material exists on
        disk and the call presents the greffer's client cert."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mock_requests.RequestException = requests.RequestException

        mod.change_status('instance-42', 'running')

        mock_requests.post.assert_called_once_with(
            'https://test.greffon.io/api/greffer/instances/instance-42/',
            json={'status': 'running'},
            timeout=mod.REQUEST_TIMEOUT,
            verify=mod.CA_PATH,
            cert=(mod.CERT_PATH, mod.KEY_PATH),
        )

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=True)
    @patch('apps.utils.greffon.base_server.requests')
    def test_change_status_returns_response(self, mock_requests, mock_exists):
        """change_status() returns the response from requests.post."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mock_requests.RequestException = requests.RequestException

        expected_response = MagicMock()
        mock_requests.post.return_value = expected_response

        result = mod.change_status('instance-42', 'stopped')

        self.assertIs(result, expected_response)

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=True)
    def test_change_status_returns_none_on_network_error(self, mock_exists):
        """A transport error (e.g. manager briefly unreachable) must not crash
        the monitoring thread — log and return None."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'

        with patch('apps.utils.greffon.base_server.requests') as mock_requests:
            mock_requests.RequestException = requests.RequestException
            mock_requests.post.side_effect = requests.Timeout()
            result = mod.change_status('instance-42', 'running')

        self.assertIsNone(result)


class WaitForRegistrationTests(unittest.TestCase):
    def test_returns_false_before_register_completes(self):
        import apps.utils.greffon.base_server as mod

        mod._registered.clear()
        try:
            # Non-blocking check — event not yet set.
            self.assertFalse(mod.wait_for_registration(timeout=0))
        finally:
            mod._registered.clear()

    def test_returns_true_after_set(self):
        import apps.utils.greffon.base_server as mod

        mod._registered.clear()
        mod._registered.set()
        try:
            self.assertTrue(mod.wait_for_registration(timeout=0))
        finally:
            mod._registered.clear()
