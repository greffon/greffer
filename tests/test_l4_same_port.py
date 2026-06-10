"""Tests for same_port + sticky L4 port allocation (greffer 0.3.0).

same_port publishes the allocated host port AS the container port (host P ->
container P) so an app advertises exactly what it binds; sticky allocation
persists the host port per (instance, port_name) so the L4 endpoint survives
restarts. Spans:

  - apps/utils/docker/compose.py     — same_port publish branch
  - apps/utils/os/network.py         — is_port_free, allocate_ports_in_range
  - apps/utils/greffon/sticky_ports.py — sidecar load/save
  - apps/utils/greffon/repository.py — sticky L4 allocation in get_greffon_info
"""
import copy
import socket
import tempfile
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import SAMPLE_CERT


# ---------------------------------------------------------------------------
# 1. compose publish — same_port rewrites the container side to port_host
# ---------------------------------------------------------------------------

class SamePortPublishTests(TestCase):
    def _info(self, same_port):
        return {
            'id': 'test-instance',
            'ports': [{
                'port_container': '51820',
                'container_name': 'wireguard',
                'port_name': 'wireguard_51820',
                'protocol': 'udp',
                'exposure_tier': 'l4',
                'port_host': 40000,
                'same_port': same_port,
            }],
            'volumes': {},
            'networks': {'net1': {'value': 'greffon_net', 'containers': []}},
            'internal_network': 'greffon_net',
            'services': {
                'wireguard': {'value': 'wireguard'},
                'greffon_nginx': {'value': 'greffon_nginx'},
            },
        }

    def _compose(self):
        return {'version': '3', 'services': {
            'wireguard': {'image': 'wg', 'ports': ['51820:51820/udp']}}}

    @patch('apps.utils.docker.compose.client')
    def test_same_port_publishes_host_port_on_both_sides(self, _mock_client):
        """same_port=True -> 0.0.0.0:<host>:<host>/udp (container side is the
        allocated host port, not the declared 51820)."""
        from apps.utils.docker.compose import create_compose_template_from_greffon
        result = create_compose_template_from_greffon(self._compose(), self._info(True))
        wg_ports = result['services']['wireguard']['ports']
        self.assertIn(
            '0.0.0.0:{{ports[0].port_host}}:{{ports[0].port_host}}/udp', wg_ports)
        self.assertNotIn('0.0.0.0:{{ports[0].port_host}}:51820/udp', wg_ports)

    @patch('apps.utils.docker.compose.client')
    def test_default_publishes_declared_container_port(self, _mock_client):
        """same_port=False (default) keeps host -> declared container port."""
        from apps.utils.docker.compose import create_compose_template_from_greffon
        result = create_compose_template_from_greffon(self._compose(), self._info(False))
        wg_ports = result['services']['wireguard']['ports']
        self.assertIn('0.0.0.0:{{ports[0].port_host}}:51820/udp', wg_ports)
        self.assertNotIn(
            '0.0.0.0:{{ports[0].port_host}}:{{ports[0].port_host}}/udp', wg_ports)


# ---------------------------------------------------------------------------
# 2. network helpers — targeted probe + range allocation
# ---------------------------------------------------------------------------

class NetworkHelperTests(TestCase):
    def test_is_port_free_true_then_false_when_bound(self):
        from apps.utils.os.network import is_port_free, get_free_ports
        port = get_free_ports(numbers=1, protocol='tcp')[0]
        self.assertTrue(is_port_free('127.0.0.1', port, 'tcp'))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', port))
        try:
            self.assertFalse(is_port_free('127.0.0.1', port, 'tcp'))
        finally:
            s.close()

    def test_allocate_in_range_skips_reserved_and_stays_in_range(self):
        from apps.utils.os.network import allocate_ports_in_range
        ports = allocate_ports_in_range(
            '127.0.0.1', 2, 21000, 21050, protocol='tcp', reserved={21000, 21001})
        self.assertEqual(len(ports), 2)
        self.assertTrue(all(21000 <= p <= 21050 for p in ports))
        self.assertNotIn(21000, ports)
        self.assertNotIn(21001, ports)
        self.assertEqual(len(set(ports)), 2)  # distinct

    def test_allocate_in_range_raises_when_exhausted(self):
        from apps.utils.os.network import allocate_ports_in_range
        with self.assertRaises(RuntimeError):
            allocate_ports_in_range('127.0.0.1', 3, 21100, 21101, protocol='tcp')


# ---------------------------------------------------------------------------
# 3. sticky_ports sidecar — round-trip + tolerance
# ---------------------------------------------------------------------------

class StickyPortsSidecarTests(TestCase):
    def test_save_then_load_round_trip(self):
        from apps.utils.greffon import sticky_ports
        with tempfile.TemporaryDirectory() as root:
            sticky_ports.save(root, 'inst-1', {'wg_51820': 40000})
            self.assertEqual(sticky_ports.load(root, 'inst-1'), {'wg_51820': 40000})

    def test_load_missing_returns_empty(self):
        from apps.utils.greffon import sticky_ports
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(sticky_ports.load(root, 'nope'), {})

    def test_load_tolerates_corrupt_sidecar(self):
        import os
        from apps.utils.greffon import sticky_ports
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'inst-2'))
            with open(os.path.join(root, 'inst-2', 'l4_ports.json'), 'w') as f:
                f.write('{not json')
            self.assertEqual(sticky_ports.load(root, 'inst-2'), {})


# ---------------------------------------------------------------------------
# 4. get_greffon_info — sticky L4 allocation
# ---------------------------------------------------------------------------

class StickyAllocationTests(TestCase):
    def _greffon(self, same_port=True):
        return {
            'id': 'inst-sticky', 'repository_url': 'https://x/c.yml',
            'cert': copy.deepcopy(SAMPLE_CERT), 'configurations': [],
            'ports': {'wireguard_51820': {
                'exposure_tier': 'l4', 'protocol': 'udp',
                'same_port': same_port, 'url': None}},
        }

    def _compose(self):
        return {'version': '3', 'services': {
            'wireguard': {'image': 'wg', 'ports': ['51820:51820/udp']}}}

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.greffon.repository.is_port_free', return_value=True)
    @patch('apps.utils.greffon.repository.allocate_ports_in_range')
    def test_reuses_sticky_port_when_free(self, mock_alloc, _mock_free, mock_sticky):
        """A persisted port that is still free is reused — no fresh allocation."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {'wireguard_51820': 47777}
        result = get_greffon_info(self._compose(), self._greffon())
        port = result['ports'][0]
        self.assertEqual(port['port_host'], 47777)
        mock_alloc.assert_not_called()
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 47777})

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.greffon.repository.is_port_free', return_value=False)
    @patch('apps.utils.greffon.repository.allocate_ports_in_range', return_value=[40001])
    def test_allocates_fresh_when_sticky_port_taken(self, _mock_alloc, _mock_free, mock_sticky):
        """A persisted port that is no longer free falls back to a fresh one
        from the L4 range, and the new value is persisted."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {'wireguard_51820': 47777}
        result = get_greffon_info(self._compose(), self._greffon())
        self.assertEqual(result['ports'][0]['port_host'], 40001)
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 40001})

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.greffon.repository.allocate_ports_in_range', return_value=[40002])
    def test_first_start_allocates_from_range_and_persists(self, _mock_alloc, mock_sticky):
        """No prior sticky entry -> allocate from the L4 range, persist it."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {}
        result = get_greffon_info(self._compose(), self._greffon())
        self.assertEqual(result['ports'][0]['port_host'], 40002)
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 40002})
