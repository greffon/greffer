"""Tests for same_port + sticky L4 port allocation.

same_port publishes the allocated host port AS the container port (host P ->
container P) so an app advertises exactly what it binds; sticky allocation
persists the host port per (instance, port_name) so the L4 endpoint survives
restarts. Cross-instance conflict avoidance comes from the docker daemon (not a
netns-blind bind-probe). Spans:

  - apps/utils/docker/compose.py     — same_port publish branch
  - apps/utils/docker/l4_ports.py    — published_l4_ports, lowest_free_port
  - apps/utils/greffon/sticky_ports.py — sidecar load/save
  - apps/utils/greffon/repository.py — L4 allocation in get_greffon_info
"""
import copy
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from tests.helpers import SAMPLE_CERT


# ---------------------------------------------------------------------------
# 1. compose publish — same_port rewrites the container side to port_host
# ---------------------------------------------------------------------------

class SamePortPublishTests(TestCase):
    def _info(self, same_port, l4_bind_host='0.0.0.0'):
        return {
            'id': 'test-instance',
            'l4_bind_host': l4_bind_host,
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
    def test_same_port_proxy_publishes_host_port_on_both_sides(self, _mock_client):
        """proxy + same_port=True -> 0.0.0.0:<host>:<host>/udp (container side is
        the allocated host port, not the declared 51820). In proxy mode the
        public port IS the host port, so {{ instance_l4_port }} == port_host."""
        from apps.utils.docker.compose import create_compose_template_from_greffon
        result = create_compose_template_from_greffon(self._compose(), self._info(True))
        wg_ports = result['services']['wireguard']['ports']
        self.assertIn(
            '0.0.0.0:{{ports[0].port_host}}:{{ports[0].port_host}}/udp', wg_ports)
        self.assertNotIn('0.0.0.0:{{ports[0].port_host}}:51820/udp', wg_ports)

    @patch('apps.utils.docker.compose.client')
    def test_same_port_tunnel_publishes_instance_l4_port_container_side(self, _mock_client):
        """tunnel + same_port=True -> 127.0.0.1:<host>:{{ instance_l4_port }}/udp.

        In tunnel mode the public port is the rathole relay's tunnel_port
        (manager-allocated, handed off as instance_l4_port); the host port_host
        is just the loopback port the rathole-client dials. The container side
        must be the advertised relay port so the app binds the SAME port it is
        advertised on, NOT the greffer-local port_host (which would leave the
        app listening on the wrong port). Binds 127.0.0.1 (tunnel)."""
        from apps.utils.docker.compose import create_compose_template_from_greffon
        result = create_compose_template_from_greffon(
            self._compose(), self._info(True, l4_bind_host='127.0.0.1'))
        wg_ports = result['services']['wireguard']['ports']
        self.assertIn(
            '127.0.0.1:{{ports[0].port_host}}:{{ instance_l4_port }}/udp', wg_ports)
        # NOT the proxy form (container side = port_host) and NOT the declared port.
        self.assertNotIn(
            '127.0.0.1:{{ports[0].port_host}}:{{ports[0].port_host}}/udp', wg_ports)
        self.assertNotIn('127.0.0.1:{{ports[0].port_host}}:51820/udp', wg_ports)

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
# 2. l4_ports — docker-daemon enumeration + range pick (replaces the
#    netns-blind bind-probe)
# ---------------------------------------------------------------------------

def _fake_container(ports, project=None):
    """A docker-py-ish container whose ``attrs`` mirror ``/containers/json``."""
    container = Mock()
    labels = {'com.docker.compose.project': project} if project else {}
    container.attrs = {'Ports': ports, 'Labels': labels}
    return container


class PublishedL4PortsTests(TestCase):
    @patch('apps.utils.docker.l4_ports.client')
    def test_collects_running_published_ports_by_proto(self, mock_client):
        from apps.utils.docker.l4_ports import published_l4_ports
        mock_client.containers.list.return_value = [
            _fake_container([{'PublicPort': 20000, 'Type': 'udp', 'IP': '0.0.0.0'}]),
            _fake_container([{'PublicPort': 20001, 'Type': 'tcp', 'IP': '0.0.0.0'}]),
        ]
        self.assertEqual(
            published_l4_ports(20000, 29999), {'udp': {20000}, 'tcp': {20001}})

    @patch('apps.utils.docker.l4_ports.client')
    def test_ignores_out_of_range_and_unpublished(self, mock_client):
        from apps.utils.docker.l4_ports import published_l4_ports
        mock_client.containers.list.return_value = [
            _fake_container([
                {'PublicPort': 8080, 'Type': 'tcp'},     # below the L4 range
                {'PrivatePort': 51820, 'Type': 'udp'},   # not published
                {'PublicPort': 20005, 'Type': 'udp'},    # in range
            ]),
        ]
        self.assertEqual(published_l4_ports(20000, 29999), {'udp': {20005}})

    @patch('apps.utils.docker.l4_ports.client')
    def test_collapses_hostip(self, mock_client):
        """A port published on 0.0.0.0, 127.0.0.1, and :: (v6) counts once."""
        from apps.utils.docker.l4_ports import published_l4_ports
        mock_client.containers.list.return_value = [
            _fake_container([
                {'PublicPort': 20000, 'Type': 'udp', 'IP': '0.0.0.0'},
                {'PublicPort': 20000, 'Type': 'udp', 'IP': '127.0.0.1'},
                {'PublicPort': 20000, 'Type': 'udp', 'IP': '::'},
            ]),
        ]
        self.assertEqual(published_l4_ports(20000, 29999), {'udp': {20000}})

    @patch('apps.utils.docker.l4_ports.client')
    def test_uses_sparse_list(self, mock_client):
        """Must call list(sparse=True): the default sparse=False yields the
        inspect attrs shape (ports under NetworkSettings.Ports) this parse does
        not read, which would silently return {} and reintroduce the collision."""
        from apps.utils.docker.l4_ports import published_l4_ports
        mock_client.containers.list.return_value = []
        published_l4_ports(20000, 29999)
        mock_client.containers.list.assert_called_once_with(sparse=True)

    @patch('apps.utils.docker.l4_ports.client')
    def test_excludes_own_project(self, mock_client):
        from apps.utils.docker.l4_ports import published_l4_ports
        mock_client.containers.list.return_value = [
            _fake_container([{'PublicPort': 20000, 'Type': 'udp'}], project='me'),
            _fake_container([{'PublicPort': 20001, 'Type': 'udp'}], project='other'),
        ]
        self.assertEqual(
            published_l4_ports(20000, 29999, exclude_project='me'),
            {'udp': {20001}})

    @patch('apps.utils.docker.l4_ports.client')
    def test_daemon_error_raises_unavailable(self, mock_client):
        """A daemon error must NOT degrade to an empty set (which would blindly
        reissue range_start); it raises so the start fails cleanly."""
        from docker.errors import DockerException
        from apps.utils.docker.l4_ports import (
            L4PortsUnavailable, published_l4_ports)
        mock_client.containers.list.side_effect = DockerException('boom')
        with self.assertRaises(L4PortsUnavailable):
            published_l4_ports(20000, 29999)

    @patch('apps.utils.docker.l4_ports.client')
    def test_oserror_raises_unavailable(self, mock_client):
        """A dead unix socket can raise a bare OSError, not a DockerException."""
        from apps.utils.docker.l4_ports import (
            L4PortsUnavailable, published_l4_ports)
        mock_client.containers.list.side_effect = OSError('socket gone')
        with self.assertRaises(L4PortsUnavailable):
            published_l4_ports(20000, 29999)

    @patch('apps.utils.docker.l4_ports.client')
    def test_requests_connection_error_raises_unavailable(self, mock_client):
        """docker-py does not wrap list() transport failures, so a raw requests
        ConnectionError must also map to L4PortsUnavailable."""
        import requests
        from apps.utils.docker.l4_ports import (
            L4PortsUnavailable, published_l4_ports)
        mock_client.containers.list.side_effect = \
            requests.exceptions.ConnectionError('refused')
        with self.assertRaises(L4PortsUnavailable):
            published_l4_ports(20000, 29999)

    @patch('apps.utils.docker.l4_ports.client')
    def test_container_with_null_ports_contributes_nothing(self, mock_client):
        """A container whose Ports is null (not []) must be skipped, not crash."""
        from apps.utils.docker.l4_ports import published_l4_ports
        container = Mock()
        container.attrs = {'Ports': None, 'Labels': {}}
        mock_client.containers.list.return_value = [container]
        self.assertEqual(published_l4_ports(20000, 29999), {})

    def test_ttl_env_invalid_falls_back_to_default(self):
        """A non-numeric TTL must not crash import; fall back to 300."""
        import apps.utils.docker.l4_ports as l4p
        with patch.dict('os.environ',
                        {'GREFFER_L4_PENDING_TTL_SECONDS': 'abc'}):
            self.assertEqual(l4p._pending_ttl_seconds(), 300.0)

    def test_ttl_env_nonpositive_falls_back_to_default(self):
        """A non-positive TTL would disable the guard; fall back to 300."""
        import apps.utils.docker.l4_ports as l4p
        with patch.dict('os.environ',
                        {'GREFFER_L4_PENDING_TTL_SECONDS': '-5'}):
            self.assertEqual(l4p._pending_ttl_seconds(), 300.0)


class LowestFreePortTests(TestCase):
    def test_picks_lowest_free(self):
        from apps.utils.docker.l4_ports import lowest_free_port
        self.assertEqual(lowest_free_port(20000, 29999, {20000, 20001}), 20002)

    def test_none_when_exhausted(self):
        from apps.utils.docker.l4_ports import lowest_free_port
        self.assertIsNone(lowest_free_port(20000, 20001, {20000, 20001}))


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
    def setUp(self):
        # The allocation guard's pending set is a module global; isolate tests.
        import apps.utils.docker.l4_ports as l4p
        l4p._pending.clear()

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
    @patch('apps.utils.docker.l4_ports.published_l4_ports', return_value={})
    def test_reuses_sticky_port_when_free(self, _occ, mock_sticky):
        """A persisted port nothing else holds is reused — no rotation."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {'wireguard_51820': 27777}
        result = get_greffon_info(self._compose(), self._greffon())
        self.assertEqual(result['ports'][0]['port_host'], 27777)
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 27777})

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports',
           return_value={'udp': {27777}})
    def test_first_start_picks_lowest_free(self, _occ, mock_sticky):
        """No prior sticky entry -> lowest free in the range, persisted."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {}
        result = get_greffon_info(self._compose(), self._greffon())
        self.assertEqual(result['ports'][0]['port_host'], 20000)
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 20000})

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports',
           return_value={'udp': {27777}})
    def test_plain_l4_rotates_when_sticky_taken(self, _occ, mock_sticky):
        """A plain (same_port=False) L4 port whose sticky value is now held
        rotates to the lowest free port — harmless, no error."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {'wireguard_51820': 27777}
        result = get_greffon_info(self._compose(), self._greffon(same_port=False))
        self.assertEqual(result['ports'][0]['port_host'], 20000)
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 20000})

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports',
           return_value={'udp': {27777}})
    def test_proxy_same_port_conflict_raises(self, _occ, mock_sticky):
        """Proxy + same_port: a taken pinned port is a hard error, never a
        silent rotation (the app baked the port into client configs)."""
        from apps.utils.docker.l4_ports import L4SamePortConflict
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {'wireguard_51820': 27777}
        with self.assertRaises(L4SamePortConflict):
            get_greffon_info(self._compose(), self._greffon(same_port=True))
        mock_sticky.save.assert_not_called()

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports',
           return_value={'udp': {27777}})
    def test_tunnel_same_port_rotates_without_error(self, _occ, mock_sticky):
        """Tunnel + same_port: port_host is only the loopback the rathole-client
        dials, so a taken pinned port rotates freely (invisible to clients)."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {'wireguard_51820': 27777}
        result = get_greffon_info(
            self._compose(), self._greffon(same_port=True),
            l4_bind_host='127.0.0.1')
        self.assertEqual(result['ports'][0]['port_host'], 20000)

    @patch.dict('os.environ', {
        'GREFFER_L4_PORT_RANGE_START': '20000',
        'GREFFER_L4_PORT_RANGE_END': '20000'})
    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports',
           return_value={'udp': {20000}})
    def test_exhausted_range_raises(self, _occ, mock_sticky):
        """A single-port range fully occupied -> l4_port_range_exhausted."""
        from apps.utils.docker.l4_ports import L4PortRangeExhausted
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {}
        with self.assertRaises(L4PortRangeExhausted):
            get_greffon_info(self._compose(), self._greffon())

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports', return_value={})
    @patch('apps.utils.greffon.repository.get_free_ports')
    def test_l4_path_does_not_bind_a_socket(self, mock_free_ports, _occ, mock_sticky):
        """The L4 path allocates from the daemon view only — it never invokes
        the Tier-A socket binder (the old netns-blind probe is gone)."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {}
        get_greffon_info(self._compose(), self._greffon())
        mock_free_ports.assert_not_called()

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.client')
    def test_rotates_off_a_foreign_running_port_end_to_end(self, mock_client, mock_sticky):
        """Headline bug, through the REAL published_l4_ports parse: a foreign
        running container holds this instance's sticky port, so the allocator
        sees it occupied and rotates a plain-L4 port to the next free number."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_client.containers.list.return_value = [
            _fake_container(
                [{'PublicPort': 20000, 'Type': 'udp', 'IP': '0.0.0.0'}],
                project='other-instance'),
        ]
        mock_sticky.load.return_value = {'wireguard_51820': 20000}
        result = get_greffon_info(self._compose(), self._greffon(same_port=False))
        self.assertEqual(result['ports'][0]['port_host'], 20001)
        mock_client.containers.list.assert_called_once_with(sparse=True)
        mock_sticky.save.assert_called_once_with(
            '/data', 'inst-sticky', {'wireguard_51820': 20001})

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.client')
    def test_keeps_own_running_port_on_redeploy(self, mock_client, mock_sticky):
        """Re-deploy: this instance's OWN running container holds 20000;
        exclude_project skips it, so even a proxy same_port instance reuses
        20000 instead of hard-erroring on itself."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_client.containers.list.return_value = [
            _fake_container(
                [{'PublicPort': 20000, 'Type': 'udp', 'IP': '0.0.0.0'}],
                project='inst-sticky'),  # OUR compose project (== instance id)
        ]
        mock_sticky.load.return_value = {'wireguard_51820': 20000}
        result = get_greffon_info(self._compose(), self._greffon(same_port=True))
        self.assertEqual(result['ports'][0]['port_host'], 20000)

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports')
    def test_daemon_unavailable_propagates_and_does_not_persist(self, mock_pub, mock_sticky):
        """A daemon-enumeration failure bubbles out of get_greffon_info (the
        controller turns it into a 503) and nothing half-allocated is saved."""
        from apps.utils.docker.l4_ports import L4PortsUnavailable
        from apps.utils.greffon.repository import get_greffon_info
        mock_pub.side_effect = L4PortsUnavailable('down')
        mock_sticky.load.return_value = {}
        with self.assertRaises(L4PortsUnavailable):
            get_greffon_info(self._compose(), self._greffon())
        mock_sticky.save.assert_not_called()

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports', return_value={})
    def test_enumeration_scoped_to_own_project_and_range(self, mock_pub, mock_sticky):
        """The reserved-set query excludes this instance's own project and uses
        the configured L4 range."""
        from apps.utils.greffon.repository import get_greffon_info
        mock_sticky.load.return_value = {}
        get_greffon_info(self._compose(), self._greffon())
        mock_pub.assert_called_once_with(20000, 29999, exclude_project='inst-sticky')

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.docker.l4_ports.published_l4_ports',
           return_value={'udp': {20000}})
    def test_two_l4_ports_get_distinct_free_numbers(self, _occ, mock_sticky):
        """Two L4 udp ports in one instance with 20000 occupied get 20001 and
        20002 — the per-call batch set prevents handing the same number twice."""
        from apps.utils.greffon.repository import get_greffon_info
        compose = {'version': '3', 'services': {'wg': {
            'image': 'wg', 'ports': ['51820:51820/udp', '51821:51821/udp']}}}
        greffon = {
            'id': 'inst-multi', 'repository_url': 'https://x/c.yml',
            'cert': copy.deepcopy(SAMPLE_CERT), 'configurations': [],
            'ports': {
                'wg_51820': {'exposure_tier': 'l4', 'protocol': 'udp',
                             'same_port': False, 'url': None},
                'wg_51821': {'exposure_tier': 'l4', 'protocol': 'udp',
                             'same_port': False, 'url': None},
            }}
        mock_sticky.load.return_value = {}
        result = get_greffon_info(compose, greffon)
        self.assertEqual(
            sorted(p['port_host'] for p in result['ports']), [20001, 20002])

    def test_pending_reservation_blocks_a_concurrent_pick(self):
        """A port handed to ANOTHER instance's in-flight start (marked pending,
        not yet daemon-visible) is avoided by a concurrent start."""
        import apps.utils.docker.l4_ports as l4p
        from apps.utils.greffon.repository import get_greffon_info
        l4p.mark_pending('other-instance', 'udp', 20000)
        with patch('apps.utils.docker.l4_ports.published_l4_ports',
                   return_value={}), \
                patch('apps.utils.greffon.repository.sticky_ports') as mock_sticky:
            mock_sticky.load.return_value = {}
            result = get_greffon_info(self._compose(), self._greffon())
        self.assertEqual(result['ports'][0]['port_host'], 20001)

    def test_own_pending_does_not_block_restart(self):
        """An instance restarting within the TTL must reclaim its OWN port: its
        own pending reservation (its running container is excluded from the
        daemon view) must not count against it, or a proxy same_port instance
        would 409 on itself and a plain L4 one would rotate off its endpoint."""
        import apps.utils.docker.l4_ports as l4p
        from apps.utils.greffon.repository import get_greffon_info
        l4p.mark_pending('inst-sticky', 'udp', 20000)  # this instance's own
        with patch('apps.utils.docker.l4_ports.published_l4_ports',
                   return_value={}), \
                patch('apps.utils.greffon.repository.sticky_ports') as mock_sticky:
            mock_sticky.load.return_value = {'wireguard_51820': 20000}
            result = get_greffon_info(
                self._compose(), self._greffon(same_port=True))
        self.assertEqual(result['ports'][0]['port_host'], 20000)

    def test_pending_pruned_when_daemon_visible(self):
        """Once a port shows up as occupied (its container bound it), its
        pending reservation is dropped (no permanent false reservation)."""
        import apps.utils.docker.l4_ports as l4p
        l4p.mark_pending('x', 'udp', 20000)
        self.assertEqual(
            l4p.pending_and_prune({'udp': {20000}}, exclude_instance='y'), {})

    def test_pending_expires_after_ttl(self):
        """A reservation whose container never binds is freed after the TTL,
        bounding the leak from a failed start."""
        import time
        import apps.utils.docker.l4_ports as l4p
        l4p._pending['udp'] = {20000: (time.monotonic() - 1.0, 'x')}  # expired
        self.assertEqual(
            l4p.pending_and_prune({}, exclude_instance='y'), {})
