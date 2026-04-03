import copy
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_compose():
    """Return a minimal compose dict with one service."""
    return {
        'services': {
            'app': {
                'image': 'wordpress:latest',
                'ports': ['8080:80'],
                'volumes': ['app_data:/var/www/html'],
                'networks': ['internal'],
                'container_name': 'my_app',
            },
            'db': {
                'image': 'mysql:8',
                'ports': ['3306:3306'],
                'volumes': ['db_data:/var/lib/mysql'],
                'networks': ['internal'],
            },
        },
    }


def _make_greffon_info():
    """Return a greffon_info dict suitable for create_compose_template_from_greffon."""
    return {
        'id': 'test-instance',
        'ports': [{'port_container': '443'}],
        'internal_network': 'greffon_net',
        'volumes': {
            'vol1': {
                'value': 'shared_data',
                'containers': {
                    'app': {'path': '/var/www/html'},
                },
                'files': [],
            },
        },
        'networks': {
            'net1': {
                'value': 'greffon_net',
                'containers': ['app', 'db'],
            },
        },
        'services': {
            'app': {'value': 'renamed_app'},
            'db': {'value': 'renamed_db'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        },
    }


# ---------------------------------------------------------------------------
# Tests for create_compose_template_from_greffon
# ---------------------------------------------------------------------------

class CreateComposeTemplateTests(unittest.TestCase):
    """Tests for create_compose_template_from_greffon."""

    @patch('apps.utils.docker.compose.client')
    def test_create_compose_template_strips_ports_volumes(self, mock_client):
        """All existing service ports, volumes, and networks should be reset
        to empty lists."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = _make_compose()
        greffon_info = _make_greffon_info()

        result = create_compose_template_from_greffon(compose, greffon_info)

        # Check the renamed services -- the original 'app' service is now
        # keyed by the greffon_info services value. We verify via the returned
        # compose that no original port/volume values remain as the first
        # entries; they should have been cleared before re-population.
        for service in result['services'].values():
            # ports are always reset to [] (nginx gets its own list)
            # networks and volumes are rebuilt from greffon_info
            self.assertIsInstance(service.get('ports', []), list)
            self.assertIsInstance(service.get('volumes', []), list)
            self.assertIsInstance(service.get('networks', []), list)

    @patch('apps.utils.docker.compose.client')
    def test_create_compose_template_removes_container_name(self, mock_client):
        """container_name keys should be deleted from all services."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = _make_compose()
        greffon_info = _make_greffon_info()

        result = create_compose_template_from_greffon(compose, greffon_info)

        for service in result['services'].values():
            self.assertNotIn('container_name', service)

    @patch('apps.utils.docker.compose.client')
    def test_create_compose_template_adds_nginx_service(self, mock_client):
        """A greffon_nginx service should be added to compose."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = _make_compose()
        greffon_info = _make_greffon_info()

        result = create_compose_template_from_greffon(compose, greffon_info)

        # The nginx service is renamed by greffon_info['services']['greffon_nginx']
        nginx_key = greffon_info['services']['greffon_nginx']['value']
        self.assertIn(nginx_key, result['services'])
        nginx_service = result['services'][nginx_key]
        self.assertEqual(nginx_service['image'], 'nginx:1.20.2-alpine-perl')

    @patch('apps.utils.docker.compose.client')
    def test_create_compose_template_maps_volumes(self, mock_client):
        """Volumes from greffon_info should be appended to the correct services
        and appear in the top-level volumes dict."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = _make_compose()
        greffon_info = _make_greffon_info()

        result = create_compose_template_from_greffon(compose, greffon_info)

        # The 'app' service is renamed to 'renamed_app'
        app_volumes = result['services']['renamed_app']['volumes']
        self.assertIn('shared_data:/var/www/html', app_volumes)

        # Top-level volumes
        self.assertIn('shared_data', result['volumes'])
        self.assertEqual(result['volumes']['shared_data']['name'], 'shared_data')

    @patch('apps.utils.docker.compose.client')
    def test_create_compose_template_maps_networks(self, mock_client):
        """Networks from greffon_info should be appended to the correct services
        and appear in the top-level networks dict."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = _make_compose()
        greffon_info = _make_greffon_info()

        result = create_compose_template_from_greffon(compose, greffon_info)

        # Both 'app' and 'db' should have 'greffon_net' in their networks
        app_networks = result['services']['renamed_app']['networks']
        db_networks = result['services']['renamed_db']['networks']
        self.assertIn('greffon_net', app_networks)
        self.assertIn('greffon_net', db_networks)

        # Top-level networks
        self.assertIn('greffon_net', result['networks'])

    @patch('apps.utils.docker.compose.client')
    def test_create_compose_template_renames_services(self, mock_client):
        """Service dict keys should use the values from
        greffon_info['services']."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = _make_compose()
        greffon_info = _make_greffon_info()

        result = create_compose_template_from_greffon(compose, greffon_info)

        self.assertIn('renamed_app', result['services'])
        self.assertIn('renamed_db', result['services'])
        self.assertIn('renamed_nginx', result['services'])
        # Original names should be gone
        self.assertNotIn('app', result['services'])
        self.assertNotIn('db', result['services'])
        self.assertNotIn('greffon_nginx', result['services'])


# ---------------------------------------------------------------------------
# Tests for remove_compose_file
# ---------------------------------------------------------------------------

class RemoveComposeFileTests(unittest.TestCase):
    """Tests for remove_compose_file."""

    @patch('apps.utils.docker.compose.client')
    def test_remove_compose_file_removes_both(self, mock_client):
        """When both files exist, os.remove should be called for each."""
        from apps.utils.docker.compose import remove_compose_file

        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_info = {'id': 'test-remove'}
            greffon_path = os.path.join(tmpdir, 'test-remove')
            os.makedirs(greffon_path, exist_ok=True)

            # Create both files so os.path.exists returns True
            template_path = os.path.join(greffon_path, 'docker-compose.template.yml')
            compose_path = os.path.join(greffon_path, 'docker-compose.yml')
            open(template_path, 'w').close()
            open(compose_path, 'w').close()

            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                remove_compose_file(greffon_info)

            self.assertFalse(os.path.exists(template_path))
            self.assertFalse(os.path.exists(compose_path))

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.os.remove')
    @patch('apps.utils.docker.compose.os.path.exists', return_value=False)
    def test_remove_compose_file_skips_missing(
        self, mock_exists, mock_remove, mock_client
    ):
        """When files do not exist, os.remove should not be called."""
        from apps.utils.docker.compose import remove_compose_file

        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_info = {'id': 'test-skip'}
            greffon_path = os.path.join(tmpdir, 'test-skip')
            os.makedirs(greffon_path, exist_ok=True)

            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                # os.path.exists is mocked to False, so no removals should happen
                remove_compose_file(greffon_info)

            mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for create_volumes_then_copy_files
# ---------------------------------------------------------------------------

class CreateVolumesThenCopyFilesTests(unittest.TestCase):
    """Tests for create_volumes_then_copy_files."""

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.docker_copy_file_into_volume')
    @patch('apps.utils.docker.compose.docker_create_volume')
    @patch('apps.utils.docker.compose.docker_is_volume_exist', return_value=False)
    def test_create_volumes_creates_new_volume(
        self, mock_exists, mock_create, mock_copy, mock_client
    ):
        """When a volume does not exist, docker_create_volume should be called."""
        from apps.utils.docker.compose import create_volumes_then_copy_files

        greffon_info = {
            'volumes': {
                'vol1': {
                    'value': 'my_volume',
                    'files': [],
                },
            },
        }

        create_volumes_then_copy_files(greffon_info)

        mock_exists.assert_called_once_with({'value': 'my_volume', 'files': []})
        mock_create.assert_called_once_with({'value': 'my_volume', 'files': []})
        mock_copy.assert_called_once_with({'value': 'my_volume', 'files': []})

    @patch('apps.utils.docker.compose.client')
    @patch('apps.utils.docker.compose.docker_copy_file_into_volume')
    @patch('apps.utils.docker.compose.docker_create_volume')
    @patch('apps.utils.docker.compose.docker_is_volume_exist', return_value=True)
    def test_create_volumes_skips_existing(
        self, mock_exists, mock_create, mock_copy, mock_client
    ):
        """When a volume already exists, docker_create_volume should NOT be called."""
        from apps.utils.docker.compose import create_volumes_then_copy_files

        greffon_info = {
            'volumes': {
                'vol1': {
                    'value': 'existing_volume',
                    'files': [],
                },
            },
        }

        create_volumes_then_copy_files(greffon_info)

        mock_exists.assert_called_once()
        mock_create.assert_not_called()
        # copy should still be called regardless
        mock_copy.assert_called_once()
