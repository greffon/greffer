import copy
import json
import os
import tempfile
from unittest.mock import patch, MagicMock, mock_open

import yaml
from jinja2 import Template

from unittest import TestCase

from tests.helpers import SAMPLE_COMPOSE, SAMPLE_START_PAYLOAD


class ComputeInstanceContextTests(TestCase):
    """Tests for _compute_instance_context — the Jinja context helper that
    exposes ``instance_url`` (source of truth), plus the parsed-out
    ``instance_host`` / ``instance_port`` back-compat companions for
    catalogs that pre-date the manager-URL contract.

    New catalogs SHOULD prefer ``instance_url`` + inline Jinja string
    ops (e.g. ``{{ instance_url.split('://')[1] }}``); the parsed
    pieces are kept exposed because they're a public API on main
    (added in greffer commit 0a1c8aa) that external catalogs may
    depend on."""

    @patch.dict(os.environ, {'GREFFER_PUBLIC_HOST': 'worker.example.com'})
    def test_fallback_built_from_public_host_and_port_host(self):
        """No manager URL ⇒ host/port/url all built from GREFFER_PUBLIC_HOST
        + port_host. Used in greffer-direct test / dev paths where no
        public proxy fronts the greffer."""
        from apps.utils.docker.compose import _compute_instance_context

        info = _compute_instance_context({'id': 'abc', 'ports': [{'port_host': 4242}]})

        self.assertEqual(info['instance_id'], 'abc')
        self.assertEqual(info['instance_host'], 'worker.example.com')
        self.assertEqual(info['instance_port'], 4242)
        self.assertEqual(info['instance_url'], 'https://worker.example.com:4242')

    @patch.dict(os.environ, {'GREFFER_PUBLIC_HOST': 'worker.example.com', 'GREFFER_PUBLIC_SCHEME': 'http'})
    def test_honors_public_host_and_scheme_env(self):
        from apps.utils.docker.compose import _compute_instance_context

        info = _compute_instance_context({'id': 'abc', 'ports': [{'port_host': 8080}]})

        self.assertEqual(info['instance_host'], 'worker.example.com')
        self.assertEqual(info['instance_url'], 'http://worker.example.com:8080')

    @patch.dict(os.environ, {'GREFFER_PUBLIC_HOST': 'worker.example.com'})
    def test_no_ports_yields_empty_port_and_portless_url(self):
        from apps.utils.docker.compose import _compute_instance_context

        info = _compute_instance_context({'id': 'abc', 'ports': []})

        self.assertEqual(info['instance_port'], '')
        self.assertEqual(info['instance_url'], 'https://worker.example.com')

    @patch.dict(os.environ, {'GREFFER_PUBLIC_HOST': 'worker.example.com'})
    def test_jinja_render_substitutes_instance_url_in_env(self):
        """End-to-end: a compose env var containing ``{{ instance_url }}``
        — and inline split for the host-portion case — is resolved
        when the compose is rendered via Template(yaml.dump(...))."""
        from apps.utils.docker.compose import _compute_instance_context

        info = _compute_instance_context({'id': 'xyz', 'ports': [{'port_host': 5555}]})
        compose = {
            'services': {
                'app': {
                    'environment': [
                        # Catalogs that need the host[:port] use inline split.
                        "TRUSTED_DOMAINS={{ instance_url.split('://')[1] }} localhost",
                        'BASE_URL={{ instance_url }}',
                    ],
                },
            },
        }
        rendered = Template(yaml.dump(compose)).render(**info)

        self.assertIn('TRUSTED_DOMAINS=worker.example.com:5555 localhost', rendered)
        self.assertIn('BASE_URL=https://worker.example.com:5555', rendered)

    def test_does_not_clobber_existing_instance_keys(self):
        from apps.utils.docker.compose import _compute_instance_context

        pre = {'id': 'abc', 'ports': [{'port_host': 1}], 'instance_url': 'https://override'}
        info = _compute_instance_context(pre)

        self.assertEqual(info['instance_url'], 'https://override')

    def test_manager_supplied_url_wins_over_greffer_public_host(self):
        """The manager sends ``ports[0].url = https://<field-id>.my.<domain>``
        in the start payload. ``instance_url`` must surface THAT — the
        user-facing wildcard subdomain — not the greffer-direct
        ``GREFFER_PUBLIC_HOST:port_host`` form, otherwise greffons like
        Plausible bake an internal port into emails/OAuth/share-links
        and users get sent to a host that doesn't resolve from elsewhere."""
        from apps.utils.docker.compose import _compute_instance_context

        info = _compute_instance_context({
            'id': 'abc',
            'ports': [{
                'port_host': 51019,
                'url': 'https://1b1feba6-a4a5-443e-b5ce-e822e778bc99.my.greffon.local',
            }],
        })

        self.assertEqual(
            info['instance_url'],
            'https://1b1feba6-a4a5-443e-b5ce-e822e778bc99.my.greffon.local',
        )
        # Back-compat: instance_host / instance_port stay parsed from
        # the manager URL. instance_port is empty (NOT a fallback to
        # the greffer-local port_host = 51019) because the user-facing
        # URL has no explicit port. Catalogs that previously rendered
        # ``{{ instance_host }}:{{ instance_port }}`` against the
        # greffer-local form silently leaked the internal port into
        # OVERWRITEHOST-style env values; the corrected semantics
        # surface the actual user-facing port (empty for default 443).
        self.assertEqual(
            info['instance_host'],
            '1b1feba6-a4a5-443e-b5ce-e822e778bc99.my.greffon.local',
        )
        self.assertEqual(info['instance_port'], '')

    def test_manager_url_with_explicit_port_is_passed_through(self):
        """Non-default-port public deployments (operator-supplied custom
        domain like ``https://example.com:8443``): ``instance_url``
        carries it verbatim. Catalogs render whatever host:port form
        they need via inline string ops on ``instance_url``."""
        from apps.utils.docker.compose import _compute_instance_context

        info = _compute_instance_context({
            'id': 'abc',
            'ports': [{
                'port_host': 51019,
                'url': 'https://example.com:8443',
            }],
        })

        self.assertEqual(info['instance_url'], 'https://example.com:8443')
        # Back-compat: parsed pieces also surface the explicit port.
        self.assertEqual(info['instance_host'], 'example.com')
        self.assertEqual(info['instance_port'], '8443')

    @patch.dict(os.environ, {'GREFFER_PUBLIC_HOST': 'worker.example.com'})
    def test_malformed_manager_url_falls_back(self):
        """Non-string, missing scheme, or otherwise malformed values in
        ``ports[0].url`` should not leak into ``instance_url`` —
        otherwise greffons render broken BASE_URL / share links /
        OAuth callbacks. Fall back to the greffer-local URL."""
        from apps.utils.docker.compose import _compute_instance_context

        for bad in ['abc', '/foo', '', None, 12345, {'not': 'a-string'}]:
            info = _compute_instance_context({
                'id': 'abc',
                'ports': [{'port_host': 51019, 'url': bad}],
            })
            self.assertEqual(
                info['instance_url'],
                'https://worker.example.com:51019',
                f'malformed url {bad!r} should fall back',
            )


class GetNginxServiceTests(TestCase):
    """Tests for get_nginx_service."""

    @patch('apps.utils.docker.compose.client')
    def test_get_nginx_service(self, mock_client):
        from apps.utils.docker.compose import get_nginx_service

        greffon_info = {
            'ports': [
                {'port_container': '80'},
                {'port_container': '443'},
            ],
            'internal_network': 'greffon_internal_network',
        }
        result = get_nginx_service(greffon_info)

        self.assertEqual(result['image'], 'nginx:1.20.2-alpine-perl')
        self.assertEqual(result['restart'], 'unless-stopped')
        self.assertIn('greffon_internal_network', result['networks'])
        # Verify the ports template format uses Jinja2 double-brace syntax
        self.assertEqual(len(result['ports']), 2)
        self.assertIn('{{ports[0].port_host}}:80', result['ports'][0])
        self.assertIn('{{ports[1].port_host}}:443', result['ports'][1])


class ApplyConfigurationTests(TestCase):
    """Tests for apply_configuration."""

    @patch('apps.utils.docker.compose.client')
    def test_apply_configuration_json(self, mock_client):
        """JSON destination: write JSON file and add entry to volume files."""
        from apps.utils.docker.compose import apply_configuration

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                greffon_info = {
                    'id': 'test-instance-123',
                    'configurations': [
                        {
                            'value': {'db_host': 'localhost'},
                            'destinations': [
                                {
                                    'type': 'json',
                                    'name': 'config.json',
                                    'volume': 'app_data',
                                }
                            ],
                        }
                    ],
                    'volumes': {
                        'app_data': {
                            'files': [],
                        }
                    },
                }
                compose = {}
                result = apply_configuration(greffon_info, compose)

                # Verify the JSON file was written
                greffon_path = os.path.join(tmpdir, 'test-instance-123')
                file_path = os.path.join(greffon_path, 'config.json')
                self.assertTrue(os.path.exists(file_path))
                with open(file_path) as f:
                    content = json.loads(f.read())
                self.assertEqual(content, {'db_host': 'localhost'})

                # Verify the file was added to volume files
                self.assertEqual(len(result['volumes']['app_data']['files']), 1)
                self.assertEqual(
                    result['volumes']['app_data']['files'][0]['dest'],
                    'config.json',
                )
                self.assertEqual(
                    result['volumes']['app_data']['files'][0]['type'],
                    'path',
                )

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.remove_compose_file')
    def test_apply_configuration_env(self, mock_remove, mock_client):
        """Env destination: append environment variable to compose service."""
        from apps.utils.docker.compose import apply_configuration

        greffon_info = {
            'id': 'test-env',
            'configurations': [
                {
                    'value': {'value': 'my_db_host'},
                    'destinations': [
                        {
                            'type': 'env',
                            'container': 'app',
                            'key': 'DB_HOST',
                        }
                    ],
                }
            ],
            'volumes': {},
        }
        compose = {
            'services': {
                'app': {}
            }
        }
        apply_configuration(greffon_info, compose)

        self.assertIn('environment', compose['services']['app'])
        self.assertIn('DB_HOST=my_db_host', compose['services']['app']['environment'])

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.remove_compose_file')
    @patch('apps.utils.docker.compose.DataURI')
    def test_apply_configuration_file(self, mock_datauri_cls, mock_remove, mock_client):
        """File destination: decode data-URI, write binary file."""
        from apps.utils.docker.compose import apply_configuration

        mock_uri_instance = MagicMock()
        mock_uri_instance.data = b'binary-content'
        mock_datauri_cls.return_value = mock_uri_instance

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                greffon_info = {
                    'id': 'test-file',
                    'configurations': [
                        {
                            'value': {'file': 'data:application/octet-stream;base64,YmluYXJ5'},
                            'destinations': [
                                {
                                    'type': 'file',
                                    'name': 'upload.bin',
                                    'volume': 'app_data',
                                }
                            ],
                        }
                    ],
                    'volumes': {
                        'app_data': {
                            'files': [],
                        }
                    },
                }
                compose = {}
                result = apply_configuration(greffon_info, compose)

                greffon_path = os.path.join(tmpdir, 'test-file')
                file_path = os.path.join(greffon_path, 'upload.bin')
                self.assertTrue(os.path.exists(file_path))
                with open(file_path, 'rb') as f:
                    self.assertEqual(f.read(), b'binary-content')

                self.assertEqual(len(result['volumes']['app_data']['files']), 1)
                self.assertEqual(
                    result['volumes']['app_data']['files'][0]['dest'],
                    'upload.bin',
                )

    @patch('apps.utils.docker.compose.client')
    def test_apply_configuration_empty(self, mock_client):
        """No configurations key should result in a no-op."""
        from apps.utils.docker.compose import apply_configuration

        greffon_info = {'id': 'test-empty', 'volumes': {}}
        compose = {}
        result = apply_configuration(greffon_info, compose)
        self.assertEqual(result['id'], 'test-empty')


class CreateComposeTests(TestCase):
    """Tests for create_compose."""

    @patch('apps.utils.docker.compose.client')
    def test_create_compose(self, mock_client):
        """Renders Jinja2 template and writes docker-compose.yml."""
        from apps.utils.docker.compose import create_compose

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                compose = {'version': '3', 'services': {'app': {'image': 'nginx'}}}
                greffon_info = {'id': 'test-compose'}

                create_compose(compose, greffon_info)

                compose_path = os.path.join(tmpdir, 'test-compose', 'docker-compose.yml')
                self.assertTrue(os.path.exists(compose_path))
                with open(compose_path) as f:
                    content = f.read()
                self.assertIn('nginx', content)


class GetGreffonPathTests(TestCase):
    """Tests for get_greffon_path."""

    @patch('apps.utils.docker.compose.client')
    def test_get_greffon_path_creates_dir(self, mock_client):
        """Creates directory if it does not exist."""
        from apps.utils.docker.compose import get_greffon_path

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                greffon_info = {'id': 'new-instance'}
                path = get_greffon_path(greffon_info)

                expected = os.path.join(tmpdir, 'new-instance')
                self.assertEqual(path, expected)
                self.assertTrue(os.path.isdir(expected))


class StartStopTests(TestCase):
    """Tests for start and stop functions."""

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.subprocess')
    def test_start_calls_subprocess(self, mock_subprocess, mock_client):
        """start() should call subprocess.Popen with docker-compose up."""
        from apps.utils.docker.compose import start

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                greffon_info = {'id': 'test-start'}
                # Ensure the directory exists for get_greffon_path
                os.makedirs(os.path.join(tmpdir, 'test-start'), exist_ok=True)

                start(greffon_info)

                mock_subprocess.Popen.assert_called_once()
                call_args = mock_subprocess.Popen.call_args[0][0]
                self.assertEqual(call_args[0], 'docker-compose')
                self.assertEqual(call_args[1], '-f')
                self.assertIn('docker-compose.yml', call_args[2])
                self.assertEqual(call_args[3], 'up')

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.subprocess')
    def test_stop_calls_subprocess(self, mock_subprocess, mock_client):
        """stop() should call subprocess.Popen with docker-compose stop."""
        from apps.utils.docker.compose import stop

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                greffon_info = {'id': 'test-stop'}
                os.makedirs(os.path.join(tmpdir, 'test-stop'), exist_ok=True)

                stop(greffon_info)

                mock_subprocess.Popen.assert_called_once()
                call_args = mock_subprocess.Popen.call_args[0][0]
                self.assertEqual(call_args[0], 'docker-compose')
                self.assertEqual(call_args[1], '-f')
                self.assertIn('docker-compose.yml', call_args[2])
                self.assertEqual(call_args[3], 'stop')


class GetStatusTests(TestCase):
    """Tests for get_status."""

    @patch('apps.utils.docker.compose.client')
    def test_get_status_all_running(self, mock_client):
        """All containers running should return status 'running'."""
        from apps.utils.docker.compose import get_status

        container1 = MagicMock()
        container1.name = 'test-id_app_1'
        container1.status = 'running'
        container2 = MagicMock()
        container2.name = 'test-id_web_1'
        container2.status = 'running'
        mock_client.containers.list.return_value = [container1, container2]

        result = get_status('test-id')
        self.assertEqual(result['status'], 'running')

    @patch('apps.utils.docker.compose.client')
    def test_get_status_all_stopped(self, mock_client):
        """All containers stopped should return status 'stopped'."""
        from apps.utils.docker.compose import get_status

        container1 = MagicMock()
        container1.name = 'test-id_app_1'
        container1.status = 'exited'
        container2 = MagicMock()
        container2.name = 'test-id_web_1'
        container2.status = 'exited'
        mock_client.containers.list.return_value = [container1, container2]

        result = get_status('test-id')
        self.assertEqual(result['status'], 'stopped')

    @patch('apps.utils.docker.compose.client')
    def test_get_status_mixed(self, mock_client):
        """Mixed running/stopped containers should return 'unknow'."""
        from apps.utils.docker.compose import get_status

        container1 = MagicMock()
        container1.name = 'test-id_app_1'
        container1.status = 'running'
        container2 = MagicMock()
        container2.name = 'test-id_web_1'
        container2.status = 'exited'
        mock_client.containers.list.return_value = [container1, container2]

        result = get_status('test-id')
        self.assertEqual(result['status'], 'unknow')

    @patch('apps.utils.docker.compose.client')
    def test_get_status_excludes_migrate(self, mock_client):
        """Containers with 'migrate' in name should be skipped."""
        from apps.utils.docker.compose import get_status

        container_app = MagicMock()
        container_app.name = 'test-id_app_1'
        container_app.status = 'running'
        container_migrate = MagicMock()
        container_migrate.name = 'test-id_migrate_1'
        container_migrate.status = 'exited'
        mock_client.containers.list.return_value = [container_app, container_migrate]

        result = get_status('test-id')
        # The migrate container is skipped, so only the running container counts
        self.assertEqual(result['status'], 'running')
