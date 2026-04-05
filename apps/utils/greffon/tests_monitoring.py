import os
from unittest.mock import patch, MagicMock, call

from django.test import TestCase


class MonitorStatusTests(TestCase):
    """Tests for monitor_status.

    The monitor_status function has this structure:
        try:
            while True:
                ...work...
                time.sleep(delay)
        except Exception as e:
            logger.error(e)
            time.sleep(delay*2)

    StopIteration raised from time.sleep inside the loop is caught by
    'except Exception', then time.sleep(delay*2) is called. We need
    the second sleep to NOT raise so the function returns normally.
    """

    @patch('apps.utils.greffon.monitoring.time')
    @patch('apps.utils.greffon.monitoring.base_server')
    @patch('apps.utils.greffon.monitoring.compose')
    @patch('apps.utils.greffon.monitoring.os')
    def test_monitor_detects_status_change(
        self, mock_os, mock_compose, mock_base_server, mock_time
    ):
        """First loop with status 'running' should trigger change_status."""
        from apps.utils.greffon.monitoring import monitor_status

        mock_os.getenv.return_value = '/data'
        mock_os.listdir.return_value = ['instance-1']
        mock_compose.get_status.return_value = {'status': 'running'}
        # First call (inside loop) raises to break loop; second call (in except) returns normally
        mock_time.sleep.side_effect = [StopIteration, None]

        monitor_status(delay=5)

        mock_base_server.change_status.assert_called_once_with(
            'instance-1', 'running'
        )

    @patch('apps.utils.greffon.monitoring.time')
    @patch('apps.utils.greffon.monitoring.base_server')
    @patch('apps.utils.greffon.monitoring.compose')
    @patch('apps.utils.greffon.monitoring.os')
    def test_monitor_skips_unchanged(
        self, mock_os, mock_compose, mock_base_server, mock_time
    ):
        """Two loops with the same status should only call change_status once."""
        from apps.utils.greffon.monitoring import monitor_status

        mock_os.getenv.return_value = '/data'
        mock_os.listdir.return_value = ['instance-1']
        mock_compose.get_status.return_value = {'status': 'running'}

        # Allow two iterations, then break; last call is the except handler sleep
        mock_time.sleep.side_effect = [None, StopIteration, None]

        monitor_status(delay=5)

        # change_status should be called only once (first detection)
        mock_base_server.change_status.assert_called_once_with(
            'instance-1', 'running'
        )

    @patch('apps.utils.greffon.monitoring.time')
    @patch('apps.utils.greffon.monitoring.base_server')
    @patch('apps.utils.greffon.monitoring.compose')
    @patch('apps.utils.greffon.monitoring.os')
    def test_monitor_handles_exception(
        self, mock_os, mock_compose, mock_base_server, mock_time
    ):
        """When os.listdir raises an exception, the function should catch it
        and sleep for delay*2."""
        from apps.utils.greffon.monitoring import monitor_status

        mock_os.getenv.return_value = '/data'
        mock_os.listdir.side_effect = OSError('Permission denied')
        # The exception is caught, then sleep(delay*2) is called
        mock_time.sleep.return_value = None

        monitor_status(delay=5)

        # After the exception, sleep(delay*2) = sleep(10) should be called
        mock_time.sleep.assert_called_with(10)
