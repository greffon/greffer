import copy
import os
from unittest.mock import patch, MagicMock

from django.test import TestCase

from tests.helpers import SAMPLE_COMPOSE, SAMPLE_CERT, SAMPLE_START_PAYLOAD


class GetComposeFileFromRepositoryTests(TestCase):
    """Tests for get_compose_file_from_repository."""

    @patch('apps.utils.greffon.repository.requests')
    def test_get_compose_file_from_repository(self, mock_requests):
        """Mock requests.get returning YAML text, verify parsed dict."""
        from apps.utils.greffon.repository import get_compose_file_from_repository

        yaml_text = (
            "version: '3'\n"
            "services:\n"
            "  app:\n"
            "    image: wordpress:latest\n"
            "    ports:\n"
            "      - '8080:80'\n"
            "    volumes:\n"
            "      - app_data:/var/www/html\n"
            "    networks:\n"
            "      - internal\n"
            "volumes:\n"
            "  app_data: {}\n"
            "networks:\n"
            "  internal: {}\n"
        )
        mock_response = MagicMock()
        mock_response.text = yaml_text
        mock_requests.get.return_value = mock_response

        greffon = {'repository_url': 'https://example.com/docker-compose.yml'}
        result = get_compose_file_from_repository(greffon)

        mock_requests.get.assert_called_once_with('https://example.com/docker-compose.yml')
        self.assertIn('services', result)
        self.assertIn('app', result['services'])
        self.assertEqual(result['services']['app']['image'], 'wordpress:latest')


class CreateGreffonInfoTests(TestCase):
    """Tests for create_greffon_info."""

    def _call_create_greffon_info(self, compose, greffon):
        from apps.utils.greffon.repository import create_greffon_info
        return create_greffon_info(compose, greffon)

    def test_create_greffon_info_basic(self):
        """Simple compose should produce greffon_info with correct id and
        internal network and nginx volume."""
        compose = copy.deepcopy(SAMPLE_COMPOSE)
        greffon = copy.deepcopy(SAMPLE_START_PAYLOAD)

        result = self._call_create_greffon_info(compose, greffon)

        self.assertEqual(result['id'], 'test-instance-123')
        self.assertEqual(result['internal_network'], 'greffon_internal_network')
        # Nginx volume should exist
        self.assertIn('greffon_nginx', result['volumes'])
        nginx_vol = result['volumes']['greffon_nginx']
        self.assertEqual(nginx_vol['value'], 'test-instance-123_nginx_volume')
        # Cert files should be in the nginx volume files
        file_dests = [f['dest'] for f in nginx_vol['files']]
        self.assertIn('pem.crt', file_dests)
        self.assertIn('cert.key', file_dests)

    def test_create_greffon_info_ports(self):
        """Service with port '8080:80' should produce port_name='app_80'."""
        compose = copy.deepcopy(SAMPLE_COMPOSE)
        greffon = copy.deepcopy(SAMPLE_START_PAYLOAD)

        result = self._call_create_greffon_info(compose, greffon)

        port_names = [p['port_name'] for p in result['ports']]
        self.assertIn('app_80', port_names)
        # Verify the container port is extracted correctly
        port_entry = next(p for p in result['ports'] if p['port_name'] == 'app_80')
        self.assertEqual(port_entry['port_container'], '80')
        self.assertEqual(port_entry['container_name'], 'app')

    def test_create_greffon_info_volumes(self):
        """Volumes from compose should be mapped correctly in greffon_info."""
        compose = copy.deepcopy(SAMPLE_COMPOSE)
        greffon = copy.deepcopy(SAMPLE_START_PAYLOAD)

        result = self._call_create_greffon_info(compose, greffon)

        # app_data volume should exist and have the app container mapped
        self.assertIn('app_data', result['volumes'])
        vol = result['volumes']['app_data']
        self.assertIn('app', vol['containers'])
        self.assertEqual(vol['containers']['app']['path'], '/var/www/html')

    def test_compose_volumes_are_namespaced_by_instance_id(self):
        """Catalog-declared volumes must be namespaced per greffon instance so
        two instances that both declare e.g. `db_data` don't end up sharing
        the same docker volume (we observed this breaking Nextcloud ×
        GlitchTip on the same greffer)."""
        compose = copy.deepcopy(SAMPLE_COMPOSE)
        greffon = copy.deepcopy(SAMPLE_START_PAYLOAD)

        result = self._call_create_greffon_info(compose, greffon)

        # `name` stays the compose-author's raw label for reference, but
        # `value` — which is what the greffer writes into the rendered
        # compose and uses to tag the real docker volume — must be
        # prefixed with the greffon instance ID.
        vol = result['volumes']['app_data']
        self.assertEqual(vol['name'], 'app_data')
        self.assertEqual(vol['value'], f'{greffon["id"]}_app_data')

    def test_two_instances_get_distinct_volume_values(self):
        """Same compose rendered for two different greffon IDs must produce
        two distinct docker volume names."""
        compose1 = copy.deepcopy(SAMPLE_COMPOSE)
        compose2 = copy.deepcopy(SAMPLE_COMPOSE)
        g1 = copy.deepcopy(SAMPLE_START_PAYLOAD); g1['id'] = 'instance-one'
        g2 = copy.deepcopy(SAMPLE_START_PAYLOAD); g2['id'] = 'instance-two'

        r1 = self._call_create_greffon_info(compose1, g1)
        r2 = self._call_create_greffon_info(compose2, g2)

        v1 = r1['volumes']['app_data']['value']
        v2 = r2['volumes']['app_data']['value']
        self.assertEqual(v1, 'instance-one_app_data')
        self.assertEqual(v2, 'instance-two_app_data')
        self.assertNotEqual(v1, v2)

    def test_create_greffon_info_nginx(self):
        """greffon_nginx service should be added with cert files in volume."""
        compose = copy.deepcopy(SAMPLE_COMPOSE)
        greffon = copy.deepcopy(SAMPLE_START_PAYLOAD)

        result = self._call_create_greffon_info(compose, greffon)

        self.assertIn('greffon_nginx', result['services'])
        self.assertEqual(result['services']['greffon_nginx']['value'], 'greffon_nginx')

        # Nginx volume should have cert content entries
        nginx_files = result['volumes']['greffon_nginx']['files']
        cert_file = next(f for f in nginx_files if f['dest'] == 'pem.crt')
        self.assertEqual(cert_file['type'], 'content')
        self.assertEqual(cert_file['content'], SAMPLE_CERT['certificate'])

        key_file = next(f for f in nginx_files if f['dest'] == 'cert.key')
        self.assertEqual(key_file['type'], 'content')
        self.assertEqual(key_file['content'], SAMPLE_CERT['private_key'])


class GetGreffonInfoTests(TestCase):
    """Tests for get_greffon_info."""

    @patch('apps.utils.greffon.repository.get_free_ports')
    def test_get_greffon_info_allocates_ports(self, mock_get_free_ports):
        """get_greffon_info should allocate free ports to each port entry."""
        from apps.utils.greffon.repository import get_greffon_info

        mock_get_free_ports.return_value = [9000]

        compose = copy.deepcopy(SAMPLE_COMPOSE)
        greffon = copy.deepcopy(SAMPLE_START_PAYLOAD)

        result = get_greffon_info(compose, greffon)

        mock_get_free_ports.assert_called_once_with(numbers=1)
        # The first (and only) port should have port_host set
        self.assertEqual(result['ports'][0]['port_host'], 9000)
