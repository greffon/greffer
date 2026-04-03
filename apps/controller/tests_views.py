import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from tests.helpers import SAMPLE_START_PAYLOAD


# The controller views.py runs register() and async_task(monitor_status) at
# import time. We patch these before importing the views module.
@patch('apps.utils.greffon.base_server.register')
@patch('apps.controller.views.async_task')
class StartGreffonViewTests(TestCase):
    """Tests for POST /api/controller/start/"""

    def setUp(self):
        import apps.utils.auth as auth_module
        auth_module.token = 'test-token'

    def tearDown(self):
        import apps.utils.auth as auth_module
        auth_module.token = None

    @patch('apps.controller.views.compose')
    @patch('apps.controller.views.conf')
    @patch('apps.controller.views.repository')
    def test_start_greffon_success(
        self, mock_repo, mock_conf, mock_compose, mock_async, mock_register
    ):
        """Valid start request should call the full orchestration chain and
        return ports."""
        mock_repo.get_compose_file_from_repository.return_value = {
            'services': {'app': {'image': 'nginx', 'ports': ['80:80']}}
        }
        mock_repo.get_greffon_info.return_value = {
            'ports': [{'port_host': 9000, 'port_container': '80',
                        'container_name': 'app', 'port_name': 'app_80',
                        'url': 'https://field.greffon.io'}],
            'id': 'test-id',
        }
        mock_compose.get_compose_template.return_value = {}

        response = self.client.post(
            '/api/controller/start/',
            data=json.dumps(SAMPLE_START_PAYLOAD),
            content_type='application/json',
            HTTP_X_GREFFON_TOKEN='test-token',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('ports', data)
        mock_repo.get_compose_file_from_repository.assert_called_once()
        mock_repo.get_greffon_info.assert_called_once()
        mock_compose.get_compose_template.assert_called_once()
        mock_compose.apply_configuration.assert_called_once()
        mock_compose.create_compose.assert_called_once()
        mock_conf.create_nginx_conf.assert_called_once()
        mock_compose.create_volumes_then_copy_files.assert_called_once()
        mock_compose.start.assert_called_once()

    def test_start_greffon_invalid_payload(self, mock_async, mock_register):
        """Invalid payload (missing required fields) should return 400."""
        response = self.client.post(
            '/api/controller/start/',
            data=json.dumps({'invalid': 'data'}),
            content_type='application/json',
            HTTP_X_GREFFON_TOKEN='test-token',
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn('errors', data)

    def test_start_greffon_unauthorized(self, mock_async, mock_register):
        """Request without valid X-GREFFON-TOKEN should return 401."""
        response = self.client.post(
            '/api/controller/start/',
            data=json.dumps(SAMPLE_START_PAYLOAD),
            content_type='application/json',
            HTTP_X_GREFFON_TOKEN='wrong-token',
        )
        self.assertEqual(response.status_code, 401)


@patch('apps.utils.greffon.base_server.register')
@patch('apps.controller.views.async_task')
class StopGreffonViewTests(TestCase):
    """Tests for POST /api/controller/stop/"""

    def setUp(self):
        import apps.utils.auth as auth_module
        auth_module.token = 'test-token'

    def tearDown(self):
        import apps.utils.auth as auth_module
        auth_module.token = None

    @patch('apps.controller.views.compose')
    def test_stop_greffon_success(self, mock_compose, mock_async, mock_register):
        """Valid stop request should call compose.stop and return 200."""
        response = self.client.post(
            '/api/controller/stop/',
            data=json.dumps({'id': 'test-instance-123'}),
            content_type='application/json',
            HTTP_X_GREFFON_TOKEN='test-token',
        )
        self.assertEqual(response.status_code, 200)
        mock_compose.stop.assert_called_once()

    def test_stop_greffon_invalid_payload(self, mock_async, mock_register):
        """Missing id should return 400."""
        response = self.client.post(
            '/api/controller/stop/',
            data=json.dumps({}),
            content_type='application/json',
            HTTP_X_GREFFON_TOKEN='test-token',
        )
        self.assertEqual(response.status_code, 400)


@patch('apps.utils.greffon.base_server.register')
@patch('apps.controller.views.async_task')
class GreffonStatusViewTests(TestCase):
    """Tests for GET /api/controller/greffon/{uuid}/"""

    def setUp(self):
        import apps.utils.auth as auth_module
        auth_module.token = 'test-token'

    def tearDown(self):
        import apps.utils.auth as auth_module
        auth_module.token = None

    @patch('apps.controller.views.compose')
    def test_greffon_status_success(self, mock_compose, mock_async, mock_register):
        """Should return the result of compose.status()."""
        import uuid
        instance_id = uuid.uuid4()
        mock_compose.status.return_value = {
            'status': 'running',
            'containers': [{'status': 'running'}],
        }
        response = self.client.get(
            f'/api/controller/greffon/{instance_id}/',
            HTTP_X_GREFFON_TOKEN='test-token',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'running')
