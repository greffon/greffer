import os
from unittest.mock import patch, MagicMock, call

from django.test import TestCase


class MonitorStatusTests(TestCase):
    """Tests for monitor_status.

    The monitor_status function has this structure (post-fix):
        while True:
            logger.info("monitoring begin")
            try:
                ...work...
            except Exception:
                logger.error(...)
            time.sleep(delay)

    Per-tick try/except: the previous version placed try/except outside
    the while loop, so the first exception killed monitoring permanently.
    Tests drive iteration count by raising StopIteration from time.sleep
    at the desired stopping point; StopIteration is NOT caught by the
    inner try/except (it's outside), so it propagates out and the
    function returns normally.
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
        # First call to time.sleep (after the first tick) raises StopIteration
        # to break the while loop cleanly.
        mock_time.sleep.side_effect = [StopIteration]

        # Tests drive the while loop's termination by having time.sleep
        # raise StopIteration at the desired break point. In the new
        # placement, try/except is inside the while (around the tick
        # body only), so StopIteration from time.sleep is NOT caught —
        # it propagates out, which is exactly what we want for test
        # termination.
        with self.assertRaises(StopIteration):
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

        # Let the first two ticks run, break on the third sleep.
        mock_time.sleep.side_effect = [None, StopIteration]

        with self.assertRaises(StopIteration):
            monitor_status(delay=5)

        # change_status should be called only once (first detection).
        mock_base_server.change_status.assert_called_once_with(
            'instance-1', 'running'
        )

    @patch('apps.utils.greffon.monitoring.time')
    @patch('apps.utils.greffon.monitoring.base_server')
    @patch('apps.utils.greffon.monitoring.compose')
    @patch('apps.utils.greffon.monitoring.os')
    def test_monitor_continues_after_tick_exception(
        self, mock_os, mock_compose, mock_base_server, mock_time
    ):
        """A failing tick is logged and the next tick runs. This locks in
        the bug fix — the old version placed try/except outside the while
        loop, so one transient error (e.g. a slow manager triggering the
        new ``timeout=10`` on change_status) silently stopped monitoring
        until restart.
        """
        from apps.utils.greffon.monitoring import monitor_status

        mock_os.getenv.return_value = '/data'
        # First tick: listdir raises; second tick: listdir succeeds.
        mock_os.listdir.side_effect = [
            OSError('Permission denied'),
            ['instance-1'],
        ]
        mock_compose.get_status.return_value = {'status': 'running'}
        # Let the first (failing) tick and the second (recovering) tick
        # complete, then break.
        mock_time.sleep.side_effect = [None, StopIteration]

        with self.assertRaises(StopIteration):
            monitor_status(delay=5)

        # Monitoring recovered from the first-tick exception and reported
        # the status change on the second tick.
        mock_base_server.change_status.assert_called_once_with(
            'instance-1', 'running'
        )
