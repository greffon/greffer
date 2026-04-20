import os
import unittest
from unittest.mock import patch, MagicMock


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
        """register() should POST to the base server with correct payload, write
        cert material locally, and copy it into the nginx container on 200
        response. No cert material exists before this call, so the register call
        runs in the bootstrap verify=True branch (no client cert)."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'

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
            verify=True,
        )

        mock_write_cert.assert_any_call('pem.crt', 'CERT_DATA')
        mock_write_cert.assert_any_call('cert.key', 'KEY_DATA', mode=0o600)

        mock_copy_file.assert_any_call('test-nginx', '/root', 'pem.crt', 'CERT_DATA')
        mock_copy_file.assert_any_call('test-nginx', '/root', 'cert.key', 'KEY_DATA')

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=False)
    @patch('apps.utils.greffon.base_server._write_local_cert')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_uses_hostname_when_no_address_env(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_write_cert, mock_exists
    ):
        """When GREFFER_ADDRESS is not set, register() should resolve the local
        hostname and use its IP address."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'

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
        should retry after sleeping and succeed on the next 200 response."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'

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
    def test_mtls_when_cert_files_present(self, mock_exists):
        """Post-registration, verify against the manager-issued CA and present
        the greffer's client cert."""
        import apps.utils.greffon.base_server as mod

        auth = mod._client_auth()

        self.assertEqual(
            auth,
            {
                'verify': mod.CA_PATH,
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

        mod.change_status('instance-42', 'running')

        mock_requests.post.assert_called_once_with(
            'https://test.greffon.io/api/greffer/instances/instance-42/',
            json={'status': 'running'},
            verify=mod.CA_PATH,
            cert=(mod.CERT_PATH, mod.KEY_PATH),
        )

    @patch('apps.utils.greffon.base_server.os.path.exists', return_value=True)
    @patch('apps.utils.greffon.base_server.requests')
    def test_change_status_returns_response(self, mock_requests, mock_exists):
        """change_status() should return the response from requests.post."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'

        expected_response = MagicMock()
        mock_requests.post.return_value = expected_response

        result = mod.change_status('instance-42', 'stopped')

        self.assertIs(result, expected_response)
