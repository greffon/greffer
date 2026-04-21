import os
import unittest
from unittest.mock import patch, MagicMock, call


class RegisterTests(unittest.TestCase):
    """Tests for the register function."""

    @patch('apps.utils.greffon.base_server._fetch_and_store_crl')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_posts_to_base_server(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_fetch_crl
    ):
        """register() should POST to the base server with correct payload and
        copy certificate files into the nginx container on 200 response.
        CRL fetch is mocked out — tested separately in FetchAndStoreCrlTests."""
        import apps.utils.greffon.base_server as mod

        # Configure module-level variables
        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod.ssl_verify = False

        # Mock environment variables read inside register()
        env_values = {
            'GREFFER_ADDRESS': '10.0.0.1',
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values):
            # Mock requests.get to return 200 with certificate data
            mock_get_response = MagicMock()
            mock_get_response.status_code = 200
            mock_get_response.json.return_value = {
                'certificate': 'CERT_DATA',
                'private_key': 'KEY_DATA',
            }
            mock_requests.get.return_value = mock_get_response

            mod.register()

        # Verify POST to register endpoint
        mock_requests.post.assert_called_once_with(
            'https://test.greffon.io/api/greffer/register/greffer-abc/',
            json={
                'address': '10.0.0.1',
                'port': '8443',
                'token': 'fake-token',
                'protocol': 'https',
            },
            verify=False,
        )

        # Verify certificate files were copied into the container
        mock_copy_file.assert_any_call('test-nginx', '/root', 'pem.crt', 'CERT_DATA')
        mock_copy_file.assert_any_call('test-nginx', '/root', 'cert.key', 'KEY_DATA')
        self.assertEqual(mock_copy_file.call_count, 2)

    @patch('apps.utils.greffon.base_server._fetch_and_store_crl')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_uses_hostname_when_no_address_env(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_fetch_crl
    ):
        """When GREFFER_ADDRESS is not set, register() should resolve the local
        hostname and use its IP address."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod.ssl_verify = False

        mock_socket.gethostname.return_value = 'my-host'
        mock_socket.gethostbyname.return_value = '192.168.1.50'

        # Deliberately omit GREFFER_ADDRESS so the fallback path is taken
        env_values = {
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values, clear=False):
            # Remove GREFFER_ADDRESS if it happens to exist
            os.environ.pop('GREFFER_ADDRESS', None)

            mock_get_response = MagicMock()
            mock_get_response.status_code = 200
            mock_get_response.json.return_value = {
                'certificate': 'CERT',
                'private_key': 'KEY',
            }
            mock_requests.get.return_value = mock_get_response

            mod.register()

        # Verify the resolved IP was used in the POST payload
        post_call_kwargs = mock_requests.post.call_args
        self.assertEqual(post_call_kwargs[1]['json']['address'], '192.168.1.50')
        mock_socket.gethostname.assert_called_once()
        mock_socket.gethostbyname.assert_called_once_with('my-host')

    @patch('apps.utils.greffon.base_server._fetch_and_store_crl')
    @patch('apps.utils.greffon.base_server.time')
    @patch('apps.utils.greffon.base_server.copy_file_into_container')
    @patch('apps.utils.greffon.base_server.get_token', return_value='fake-token')
    @patch('apps.utils.greffon.base_server.requests')
    @patch('apps.utils.greffon.base_server.socket')
    def test_register_retries_cert_fetch(
        self, mock_socket, mock_requests, mock_get_token, mock_copy_file, mock_time, mock_fetch_crl
    ):
        """When the certificate endpoint returns a non-200 status, register()
        should retry after sleeping and succeed on the next 200 response."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.docker_nginx_name = 'test-nginx'
        mod.greffer_protocol = 'https'
        mod.ssl_verify = False

        env_values = {
            'GREFFER_ADDRESS': '10.0.0.1',
            'GREFFER_PORT': '8443',
            'GREFFER_ID': 'greffer-abc',
        }
        with patch.dict(os.environ, env_values):
            # First call returns 401, second call returns 200
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

        # Verify time.sleep was called between retries
        mock_time.sleep.assert_called_once_with(5)
        # Verify requests.get was called twice
        self.assertEqual(mock_requests.get.call_count, 2)


class ChangeStatusTests(unittest.TestCase):
    """Tests for the change_status function."""

    @patch('apps.utils.greffon.base_server.requests')
    def test_change_status_posts_correctly(self, mock_requests):
        """change_status() should POST to the correct URL with the status."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.ssl_verify = False

        mod.change_status('instance-42', 'running')

        mock_requests.post.assert_called_once_with(
            'https://test.greffon.io/api/greffer/instances/instance-42/',
            json={'status': 'running'},
            verify=False,
        )

    @patch('apps.utils.greffon.base_server.requests')
    def test_change_status_returns_response(self, mock_requests):
        """change_status() should return the response from requests.post."""
        import apps.utils.greffon.base_server as mod

        mod.base_server = 'https://test.greffon.io'
        mod.ssl_verify = False

        expected_response = MagicMock()
        mock_requests.post.return_value = expected_response

        result = mod.change_status('instance-42', 'stopped')

        self.assertIs(result, expected_response)
