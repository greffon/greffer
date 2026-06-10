"""Tests for HLD #4 — L4 (Tier-C) network exposure.

The L4 feature lets the manager declare raw TCP/UDP ports (``exposure_tier:
'l4'``) that bypass the nginx sidecar and are published directly on their
owning service. Coverage spans the four touched modules:

  - apps/utils/os/network.py    — UDP-aware get_free_ports
  - apps/utils/greffon/repository.py — _split_proto, protocol/tier resolution,
                                       per-protocol host-port allocation
  - apps/utils/docker/compose.py     — service-published L4 mappings,
                                       nginx-sidecar L4 exclusion
  - apps/utils/nginx/conf.py         — http_ports filtering (no L4 server block)

Mirrors the fixture/patch patterns in tests/test_compose.py,
tests/test_compose_template.py, tests/test_repository.py, and
tests/test_nginx_conf.py.
"""

import copy
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from unittest import TestCase

from tests.helpers import SAMPLE_CERT


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _l4_start_payload():
    """A start payload mirroring SAMPLE_START_PAYLOAD but with a WireGuard L4
    UDP port declared by the manager via the ``ports`` map."""
    return {
        'id': 'test-instance-123',
        'repository_url': 'https://example.com/docker-compose.yml',
        'cert': copy.deepcopy(SAMPLE_CERT),
        'configurations': [],
        'ports': {
            'wireguard_51820': {
                'exposure_tier': 'l4',
                'protocol': 'udp',
                'url': None,
            },
        },
    }


def _l4_compose():
    """Compose with a single UDP L4 service (WireGuard-style ``51820:51820/udp``)."""
    return {
        'version': '3',
        'services': {
            'wireguard': {
                'image': 'linuxserver/wireguard:latest',
                'ports': ['51820:51820/udp'],
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. get_free_ports — UDP datagram probe
# ---------------------------------------------------------------------------

class GetFreePortsUdpTests(TestCase):
    """Tests for apps.utils.os.network.get_free_ports protocol handling."""

    def test_udp_returns_ports_without_crashing(self):
        """protocol='udp' must hand out the requested number of ports using a
        datagram socket — no exception, distinct numbers."""
        from apps.utils.os.network import get_free_ports

        ports = get_free_ports(numbers=2, protocol='udp')

        self.assertEqual(len(ports), 2)
        for p in ports:
            self.assertIsInstance(p, int)
            self.assertGreater(p, 0)
        # Held open simultaneously within the call → must be distinct.
        self.assertEqual(len(set(ports)), 2)

    def test_udp_uses_datagram_socket(self):
        """The UDP path must construct a SOCK_DGRAM socket; the TCP default a
        SOCK_STREAM one. We spy on socket.socket to assert the socket type
        actually requested."""
        from apps.utils.os import network

        real_socket = socket.socket
        captured = []

        def _spy(family, type_, *args, **kwargs):
            captured.append(type_)
            return real_socket(family, type_, *args, **kwargs)

        with patch.object(network.socket, 'socket', side_effect=_spy):
            network.get_free_ports(numbers=1, protocol='udp')

        self.assertEqual(captured, [socket.SOCK_DGRAM])

    def test_tcp_default_unchanged(self):
        """No protocol arg ⇒ TCP / SOCK_STREAM (back-compat default)."""
        from apps.utils.os import network

        real_socket = socket.socket
        captured = []

        def _spy(family, type_, *args, **kwargs):
            captured.append(type_)
            return real_socket(family, type_, *args, **kwargs)

        with patch.object(network.socket, 'socket', side_effect=_spy):
            ports = network.get_free_ports(numbers=1)

        self.assertEqual(captured, [socket.SOCK_STREAM])
        self.assertEqual(len(ports), 1)


# ---------------------------------------------------------------------------
# 2. _split_proto + create_greffon_info protocol / tier resolution
# ---------------------------------------------------------------------------

class SplitProtoTests(TestCase):
    """Tests for apps.utils.greffon.repository._split_proto."""

    def test_split_udp(self):
        from apps.utils.greffon.repository import _split_proto
        self.assertEqual(_split_proto('51820/udp'), ('51820', 'udp'))

    def test_split_tcp_explicit(self):
        from apps.utils.greffon.repository import _split_proto
        self.assertEqual(_split_proto('8080/tcp'), ('8080', 'tcp'))

    def test_split_no_proto(self):
        from apps.utils.greffon.repository import _split_proto
        self.assertEqual(_split_proto('8080'), ('8080', None))

    def test_split_uppercase_proto_is_lowered(self):
        from apps.utils.greffon.repository import _split_proto
        self.assertEqual(_split_proto('51820/UDP'), ('51820', 'udp'))


class CreateGreffonInfoL4Tests(TestCase):
    """Tests for protocol / exposure_tier resolution in create_greffon_info."""

    def _call(self, compose, greffon):
        from apps.utils.greffon.repository import create_greffon_info
        return create_greffon_info(compose, greffon)

    def test_manager_declared_l4_udp(self):
        """Compose ``51820:51820/udp`` + a manager ``ports`` entry marking it
        l4/udp ⇒ port_container='51820' (suffix stripped), protocol='udp',
        exposure_tier='l4'."""
        result = self._call(_l4_compose(), _l4_start_payload())

        port = next(p for p in result['ports'] if p['port_name'] == 'wireguard_51820')
        self.assertEqual(port['port_container'], '51820')
        self.assertEqual(port['protocol'], 'udp')
        self.assertEqual(port['exposure_tier'], 'l4')
        self.assertEqual(port['container_name'], 'wireguard')

    def test_proto_suffix_stripped_from_port_container(self):
        """Even without a manager entry, the ``/udp`` suffix must be stripped
        from port_container (it's only carried in the separate protocol key)."""
        greffon = _l4_start_payload()
        greffon['ports'] = {}  # no manager declaration at all

        result = self._call(_l4_compose(), greffon)

        port = next(p for p in result['ports'] if p['port_name'] == 'wireguard_51820')
        self.assertEqual(port['port_container'], '51820')
        self.assertNotIn('/', port['port_container'])

    def test_fallback_parses_proto_from_compose(self):
        """No manager entry but ``/udp`` in the compose port string ⇒ protocol
        falls back to the parsed value. exposure_tier defaults to 'http' when
        the manager is silent."""
        greffon = _l4_start_payload()
        greffon['ports'] = {}

        result = self._call(_l4_compose(), greffon)

        port = next(p for p in result['ports'] if p['port_name'] == 'wireguard_51820')
        self.assertEqual(port['protocol'], 'udp')
        self.assertEqual(port['exposure_tier'], 'http')

    def test_default_tcp_http_for_plain_port(self):
        """Plain ``8080:80`` with no manager metadata ⇒ tcp / http defaults,
        port_container='80'."""
        compose = {
            'version': '3',
            'services': {'app': {'image': 'nginx', 'ports': ['8080:80']}},
        }
        greffon = {
            'id': 'test-instance-123',
            'repository_url': 'https://example.com/docker-compose.yml',
            'cert': copy.deepcopy(SAMPLE_CERT),
            'configurations': [],
            'ports': {},
        }

        result = self._call(compose, greffon)

        port = next(p for p in result['ports'] if p['port_name'] == 'app_80')
        self.assertEqual(port['port_container'], '80')
        self.assertEqual(port['protocol'], 'tcp')
        self.assertEqual(port['exposure_tier'], 'http')

    def test_manager_protocol_overrides_compose_parse(self):
        """Manager-declared protocol is authoritative even if the compose
        string carries no suffix."""
        compose = {
            'version': '3',
            'services': {'svc': {'image': 'x', 'ports': ['9000:9000']}},
        }
        greffon = {
            'id': 'test-instance-123',
            'repository_url': 'https://example.com/docker-compose.yml',
            'cert': copy.deepcopy(SAMPLE_CERT),
            'configurations': [],
            'ports': {'svc_9000': {'exposure_tier': 'l4', 'protocol': 'udp', 'url': None}},
        }

        result = self._call(compose, greffon)

        port = next(p for p in result['ports'] if p['port_name'] == 'svc_9000')
        self.assertEqual(port['protocol'], 'udp')
        self.assertEqual(port['exposure_tier'], 'l4')


# ---------------------------------------------------------------------------
# 3. get_greffon_info — per-protocol host-port allocation (tcp + udp coexist)
# ---------------------------------------------------------------------------

class GetGreffonInfoMixedProtocolTests(TestCase):
    """get_greffon_info must allocate a port_host for every declared port,
    batching probes per protocol so tcp and udp coexist."""

    def _call(self, compose, greffon):
        from apps.utils.greffon.repository import get_greffon_info
        return get_greffon_info(compose, greffon)

    @patch('apps.utils.greffon.repository.sticky_ports')
    @patch('apps.utils.greffon.repository.allocate_ports_in_range')
    @patch('apps.utils.greffon.repository.get_free_ports')
    def test_allocates_port_host_for_tcp_and_udp(
            self, mock_get_free_ports, mock_alloc_range, mock_sticky):
        """A compose with one Tier-A TCP and one L4 UDP port: the Tier-A port
        gets an ephemeral host port (get_free_ports), the L4 port a sticky one
        from the dedicated L4 range (allocate_ports_in_range)."""
        compose = {
            'version': '3',
            'services': {
                'app': {'image': 'nginx', 'ports': ['8080:80']},
                'wireguard': {'image': 'wg', 'ports': ['51820:51820/udp']},
            },
        }
        greffon = {
            'id': 'test-instance-123',
            'repository_url': 'https://example.com/docker-compose.yml',
            'cert': copy.deepcopy(SAMPLE_CERT),
            'configurations': [],
            'ports': {
                'wireguard_51820': {'exposure_tier': 'l4', 'protocol': 'udp', 'url': None},
            },
        }
        mock_sticky.load.return_value = {}  # no prior allocation
        mock_get_free_ports.side_effect = (
            lambda numbers, protocol='tcp': [30000 + i for i in range(numbers)])
        mock_alloc_range.return_value = [40000]

        result = self._call(compose, greffon)

        self.assertTrue(all('port_host' in p for p in result['ports']))
        tcp_port = next(p for p in result['ports'] if p['exposure_tier'] != 'l4')
        udp_port = next(p for p in result['ports'] if p['exposure_tier'] == 'l4')
        self.assertEqual(tcp_port['port_host'], 30000)  # ephemeral
        self.assertEqual(udp_port['port_host'], 40000)  # L4 range

        # Tier-A went through get_free_ports (tcp only); L4 went through the
        # range allocator (udp), and the result was persisted as sticky.
        self.assertEqual(
            [c.kwargs['protocol'] for c in mock_get_free_ports.call_args_list], ['tcp'])
        mock_alloc_range.assert_called_once()
        self.assertEqual(mock_alloc_range.call_args.kwargs['protocol'], 'udp')
        mock_sticky.save.assert_called_once()

    @patch('apps.utils.greffon.repository.get_free_ports')
    def test_two_udp_ports_get_distinct_host_ports(self, mock_get_free_ports):
        """Two UDP ports are allocated in a single batched probe
        (numbers=2, protocol='udp')."""
        compose = {
            'version': '3',
            'services': {
                'wg': {'image': 'wg', 'ports': ['51820:51820/udp', '51821:51821/udp']},
            },
        }
        greffon = {
            'id': 'test-instance-123',
            'repository_url': 'https://example.com/docker-compose.yml',
            'cert': copy.deepcopy(SAMPLE_CERT),
            'configurations': [],
            'ports': {},
        }
        mock_get_free_ports.return_value = [40000, 40001]

        result = self._call(compose, greffon)

        mock_get_free_ports.assert_called_once_with(numbers=2, protocol='udp')
        host_ports = sorted(p['port_host'] for p in result['ports'])
        self.assertEqual(host_ports, [40000, 40001])


# ---------------------------------------------------------------------------
# 4. compose template — L4 published on service, excluded from nginx sidecar
# ---------------------------------------------------------------------------

def _template_greffon_info(ports, l4_bind_host=None):
    """Build a greffon_info dict suitable for
    create_compose_template_from_greffon, parameterized by the ports list."""
    info = {
        'id': 'test-instance',
        'ports': ports,
        'internal_network': 'greffon_net',
        'volumes': {},
        'networks': {
            'net1': {'value': 'greffon_net', 'containers': []},
        },
        'services': {},
    }
    if l4_bind_host is not None:
        info['l4_bind_host'] = l4_bind_host
    return info


def _template_compose(service_names):
    """Minimal compose whose service dict matches the greffon_info services
    map, plus the implicit greffon_nginx rename entry."""
    return {
        'services': {name: {'image': 'x'} for name in service_names},
    }


class ComposeTemplateL4Tests(TestCase):
    """Tests for L4 publishing in create_compose_template_from_greffon and
    get_nginx_service."""

    @patch('apps.utils.docker.compose.client')
    def test_l4_port_published_on_service_default_bind(self, mock_client):
        """An L4 UDP port is published on its OWNING service as
        ``0.0.0.0:<host>:51820/udp`` when no l4_bind_host is set."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        ports = [{
            'port_container': '51820',
            'container_name': 'wireguard',
            'port_name': 'wireguard_51820',
            'protocol': 'udp',
            'exposure_tier': 'l4',
            'port_host': 40000,
        }]
        greffon_info = _template_greffon_info(ports)  # no l4_bind_host → default
        greffon_info['services'] = {
            'wireguard': {'value': 'renamed_wg'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        }
        greffon_info['networks']['net1']['containers'] = ['wireguard']
        compose = _template_compose(['wireguard'])

        result = create_compose_template_from_greffon(compose, greffon_info)

        wg_ports = result['services']['renamed_wg']['ports']
        self.assertIn('0.0.0.0:{{ports[0].port_host}}:51820/udp', wg_ports)

    @patch('apps.utils.docker.compose.client')
    def test_l4_port_binds_localhost_in_tunnel_mode(self, mock_client):
        """When greffon_info['l4_bind_host']='127.0.0.1' (tunnel mode), the
        published mapping binds host-internal."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        ports = [{
            'port_container': '51820',
            'container_name': 'wireguard',
            'port_name': 'wireguard_51820',
            'protocol': 'udp',
            'exposure_tier': 'l4',
            'port_host': 40000,
        }]
        greffon_info = _template_greffon_info(ports, l4_bind_host='127.0.0.1')
        greffon_info['services'] = {
            'wireguard': {'value': 'renamed_wg'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        }
        greffon_info['networks']['net1']['containers'] = ['wireguard']
        compose = _template_compose(['wireguard'])

        result = create_compose_template_from_greffon(compose, greffon_info)

        wg_ports = result['services']['renamed_wg']['ports']
        self.assertIn('127.0.0.1:{{ports[0].port_host}}:51820/udp', wg_ports)
        self.assertNotIn('0.0.0.0:{{ports[0].port_host}}:51820/udp', wg_ports)

    @patch('apps.utils.docker.compose.client')
    def test_l4_port_excluded_from_nginx_sidecar(self, mock_client):
        """The L4 port must NOT appear in the nginx sidecar's ports list."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        ports = [{
            'port_container': '51820',
            'container_name': 'wireguard',
            'port_name': 'wireguard_51820',
            'protocol': 'udp',
            'exposure_tier': 'l4',
            'port_host': 40000,
        }]
        greffon_info = _template_greffon_info(ports)
        greffon_info['services'] = {
            'wireguard': {'value': 'renamed_wg'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        }
        greffon_info['networks']['net1']['containers'] = ['wireguard']
        compose = _template_compose(['wireguard'])

        result = create_compose_template_from_greffon(compose, greffon_info)

        nginx_ports = result['services']['renamed_nginx']['ports']
        # No nginx mapping references the L4 container port.
        self.assertFalse(
            any('51820' in p for p in nginx_ports),
            f'L4 port leaked into nginx sidecar: {nginx_ports}',
        )

    @patch('apps.utils.docker.compose.client')
    def test_tier_a_goes_to_nginx_not_service(self, mock_client):
        """A Tier-A (http) port goes to the nginx sidecar and is NOT published
        directly on its owning service."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        ports = [{
            'port_container': '443',
            'container_name': 'app',
            'port_name': 'app_443',
            'protocol': 'tcp',
            'exposure_tier': 'http',
            'port_host': 30000,
        }]
        greffon_info = _template_greffon_info(ports)
        greffon_info['services'] = {
            'app': {'value': 'renamed_app'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        }
        greffon_info['networks']['net1']['containers'] = ['app']
        compose = _template_compose(['app'])

        result = create_compose_template_from_greffon(compose, greffon_info)

        # nginx sidecar carries the Tier-A port...
        nginx_ports = result['services']['renamed_nginx']['ports']
        self.assertIn('{{ports[0].port_host}}:443', nginx_ports)
        # ...and the owning service does NOT publish it directly.
        app_ports = result['services']['renamed_app']['ports']
        self.assertEqual(app_ports, [])

    @patch('apps.utils.docker.compose.client')
    def test_mixed_index_alignment_preserved(self, mock_client):
        """With an L4 port at index 0 and a Tier-A port at index 1, the nginx
        sidecar uses the Tier-A port's ORIGINAL index ({{ports[1].port_host}}),
        not a re-numbered one, so the positional template stays aligned."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        ports = [
            {
                'port_container': '51820',
                'container_name': 'wireguard',
                'port_name': 'wireguard_51820',
                'protocol': 'udp',
                'exposure_tier': 'l4',
                'port_host': 40000,
            },
            {
                'port_container': '443',
                'container_name': 'app',
                'port_name': 'app_443',
                'protocol': 'tcp',
                'exposure_tier': 'http',
                'port_host': 30000,
            },
        ]
        greffon_info = _template_greffon_info(ports)
        greffon_info['services'] = {
            'wireguard': {'value': 'renamed_wg'},
            'app': {'value': 'renamed_app'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        }
        greffon_info['networks']['net1']['containers'] = ['wireguard', 'app']
        compose = _template_compose(['wireguard', 'app'])

        result = create_compose_template_from_greffon(compose, greffon_info)

        nginx_ports = result['services']['renamed_nginx']['ports']
        # Only the Tier-A entry, and it keeps its original index 1.
        self.assertEqual(nginx_ports, ['{{ports[1].port_host}}:443'])
        # L4 published on its own service at its own index 0.
        wg_ports = result['services']['renamed_wg']['ports']
        self.assertIn('0.0.0.0:{{ports[0].port_host}}:51820/udp', wg_ports)


class GetNginxServiceL4Tests(TestCase):
    """Direct tests for get_nginx_service L4 exclusion."""

    @patch('apps.utils.docker.compose.client')
    def test_l4_excluded_index_preserved(self, mock_client):
        """get_nginx_service drops L4 ports but keeps the enumerate index over
        the full list, so the Tier-A port still resolves at its real index."""
        from apps.utils.docker.compose import get_nginx_service

        greffon = {
            'ports': [
                {'port_container': '51820', 'exposure_tier': 'l4', 'protocol': 'udp'},
                {'port_container': '443', 'exposure_tier': 'http'},
            ],
            'internal_network': 'greffon_internal_network',
        }
        result = get_nginx_service(greffon)

        self.assertEqual(result['ports'], ['{{ports[1].port_host}}:443'])


# ---------------------------------------------------------------------------
# 5. nginx conf render — no upstream/server block for L4 ports
# ---------------------------------------------------------------------------

class NginxConfL4Tests(TestCase):
    """create_nginx_conf must filter L4 ports out of http_ports so the real
    template emits no upstream/server (listen ... ssl) block for them."""

    def _render(self, ports):
        from apps.utils.nginx.conf import create_nginx_conf

        greffon_info = {'id': 'l4-nginx-test', 'ports': ports}
        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_path = os.path.join(tmpdir, greffon_info['id'])
            os.makedirs(greffon_path)
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                create_nginx_conf(greffon_info)
            with open(os.path.join(greffon_path, 'nginx.conf')) as f:
                content = f.read()
        return content, greffon_info

    def test_l4_port_gets_no_listen_block(self):
        """An L4 port's container port must not appear in any ``listen`` line,
        while the Tier-A port does."""
        ports = [
            {
                'container_name': 'wireguard', 'port_container': '51820',
                'protocol': 'udp', 'exposure_tier': 'l4',
            },
            {
                'container_name': 'app', 'port_container': '443',
                'protocol': 'tcp', 'exposure_tier': 'http',
            },
        ]
        content, greffon_info = self._render(ports)

        listen_lines = [ln for ln in content.splitlines() if 'listen' in ln]
        # The Tier-A port gets its server block.
        self.assertTrue(any('listen 443 ssl' in ln for ln in listen_lines))
        # The L4 port must NOT appear in any listen directive.
        self.assertFalse(
            any('51820' in ln for ln in listen_lines),
            f'L4 port leaked into a listen directive: {listen_lines}',
        )
        # And it gets no upstream block either.
        self.assertNotIn('upstream wireguard_51820', content)
        self.assertIn('upstream app_443', content)

        # http_ports was pre-filtered to exclude the L4 entry.
        http_port_names = [p['port_container'] for p in greffon_info['http_ports']]
        self.assertEqual(http_port_names, ['443'])

    def test_tier_a_only_still_gets_blocks(self):
        """A Tier-A-only render still emits the upstream + server blocks."""
        ports = [{
            'container_name': 'app', 'port_container': '8080',
            'protocol': 'tcp', 'exposure_tier': 'http',
        }]
        content, _ = self._render(ports)

        self.assertIn('upstream app_8080', content)
        self.assertIn('listen 8080 ssl', content)
        self.assertIn('proxy_pass http://app_8080/', content)


# ---------------------------------------------------------------------------
# 6. Regression — Tier-A-only compose unchanged (no L4 entries on services)
# ---------------------------------------------------------------------------

class TierAOnlyRegressionTests(TestCase):
    """A pure Tier-A compose must produce identical nginx-sidecar ports and
    nginx.conf blocks as before the L4 feature — nothing published on services."""

    @patch('apps.utils.docker.compose.client')
    def test_no_l4_no_service_published_ports(self, mock_client):
        """Default exposure_tier ('http') ⇒ all ports go to the nginx sidecar,
        none published directly on the owning services."""
        from apps.utils.docker.compose import create_compose_template_from_greffon

        ports = [
            {
                'port_container': '80', 'container_name': 'app',
                'port_name': 'app_80', 'protocol': 'tcp',
                'exposure_tier': 'http', 'port_host': 30000,
            },
            {
                'port_container': '3306', 'container_name': 'db',
                'port_name': 'db_3306', 'protocol': 'tcp',
                'exposure_tier': 'http', 'port_host': 30001,
            },
        ]
        greffon_info = _template_greffon_info(ports)
        greffon_info['services'] = {
            'app': {'value': 'renamed_app'},
            'db': {'value': 'renamed_db'},
            'greffon_nginx': {'value': 'renamed_nginx'},
        }
        greffon_info['networks']['net1']['containers'] = ['app', 'db']
        compose = _template_compose(['app', 'db'])

        result = create_compose_template_from_greffon(compose, greffon_info)

        # nginx sidecar carries both ports at their original indices.
        nginx_ports = result['services']['renamed_nginx']['ports']
        self.assertEqual(
            nginx_ports,
            ['{{ports[0].port_host}}:80', '{{ports[1].port_host}}:3306'],
        )
        # No port published on the owning services.
        self.assertEqual(result['services']['renamed_app']['ports'], [])
        self.assertEqual(result['services']['renamed_db']['ports'], [])

    @patch('apps.utils.docker.compose.client')
    def test_no_exposure_tier_key_defaults_to_sidecar(self, mock_client):
        """Ports lacking an exposure_tier key entirely (pre-feature shape) must
        still route to the nginx sidecar (default 'http'), not be treated as L4."""
        from apps.utils.docker.compose import get_nginx_service

        greffon = {
            'ports': [
                {'port_container': '80'},
                {'port_container': '443'},
            ],
            'internal_network': 'greffon_internal_network',
        }
        result = get_nginx_service(greffon)

        self.assertEqual(
            result['ports'],
            ['{{ports[0].port_host}}:80', '{{ports[1].port_host}}:443'],
        )

    def test_nginx_conf_tier_a_only_blocks_unchanged(self):
        """Tier-A-only nginx.conf render produces the upstream + server blocks
        exactly as the pre-feature template did (regression guard)."""
        from apps.utils.nginx.conf import create_nginx_conf

        greffon_info = {
            'id': 'regression-tier-a',
            'ports': [
                {'container_name': 'app', 'port_container': '8080',
                 'protocol': 'tcp', 'exposure_tier': 'http'},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_path = os.path.join(tmpdir, greffon_info['id'])
            os.makedirs(greffon_path)
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                create_nginx_conf(greffon_info)
            with open(os.path.join(greffon_path, 'nginx.conf')) as f:
                content = f.read()

        self.assertIn('upstream app_8080', content)
        self.assertIn('server app:8080;', content)
        self.assertIn('listen 8080 ssl', content)
        self.assertIn('proxy_pass http://app_8080/', content)
        self.assertIn('proxy_set_header X-Forwarded-Proto https;', content)


if __name__ == '__main__':
    unittest.main()
