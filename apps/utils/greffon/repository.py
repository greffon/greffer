import requests
import os
import yaml
from apps.utils.os.network import (
    get_free_ports, is_port_free, allocate_ports_in_range)
from apps.utils.greffon import sticky_ports

def get_compose_file_from_repository(greffon):
    r = requests.get(greffon['repository_url'])
    if r.status_code != 200:
        raise Exception(
            f"Failed to fetch compose file from {greffon['repository_url']}: "
            f"HTTP {r.status_code}"
        )
    return yaml.safe_load(r.text)


def _split_proto(raw):
    """'51820/udp' -> ('51820', 'udp'); '8080' -> ('8080', None)."""
    if '/' in raw:
        port, proto = raw.rsplit('/', 1)
        return port, proto.lower()
    return raw, None


def get_greffon_info(compose, greffon, l4_bind_host='0.0.0.0'):
    greffon_info = create_greffon_info(compose, greffon)
    ports = greffon_info['ports']
    greffon_path = os.getenv('GREFFON_PATH', '/data')
    instance_id = greffon['id']

    # Non-L4 (Tier-A) ports: ephemeral host port (an internal nginx upstream,
    # never user-facing). Batched per protocol so TCP/UDP are probed in their
    # own namespace and no number is handed out twice within a protocol.
    non_l4 = [i for i, p in enumerate(ports) if p.get('exposure_tier') != 'l4']
    idx_by_proto = {}
    for i in non_l4:
        idx_by_proto.setdefault(ports[i].get('protocol', 'tcp'), []).append(i)
    for proto, idxs in idx_by_proto.items():
        free = get_free_ports(numbers=len(idxs), protocol=proto)
        for idx, host_port in zip(idxs, free):
            ports[idx]['port_host'] = host_port

    # L4 (Tier-C) ports: STICKY. The host:port IS the user-facing endpoint
    # (baked into client configs and persisted inside the app), so reuse the
    # previously-allocated port when it's still free; otherwise take a fresh one
    # from the dedicated L4 range (outside the OS ephemeral range, so a stopped
    # instance's port can't be transiently stolen as a connection source port).
    # All L4 ports are sticky (same_port and plain alike): this is reuse-if-free,
    # not a hard reservation — a stopped instance's port is free for others, and
    # the live bind-probe below rotates this instance off a taken port on the
    # next start, so persisting does NOT deplete the pool. Probe + allocate on
    # the SAME interface the port will publish on (l4_bind_host), so the free
    # check matches what docker-compose will actually bind.
    l4 = [i for i, p in enumerate(ports) if p.get('exposure_tier') == 'l4']
    if l4:
        range_start = int(os.getenv('GREFFER_L4_PORT_RANGE_START', '20000'))
        range_end = int(os.getenv('GREFFER_L4_PORT_RANGE_END', '29999'))
        sticky = sticky_ports.load(greffon_path, instance_id)
        new_sticky = {}
        l4_by_proto = {}
        for i in l4:
            l4_by_proto.setdefault(ports[i].get('protocol', 'tcp'), []).append(i)
        for proto, idxs in l4_by_proto.items():
            assigned = {}   # port index -> host port
            used = set()    # host ports taken in this protocol batch
            fresh_needed = []
            for i in idxs:
                # same_port (PROXY mode only): the app advertises exactly what it
                # binds (e.g. a WebRTC media server in its ICE candidates), so the
                # PUBLISHED public host port MUST equal the container port — it
                # cannot be remapped to a range port. Pin it (and reserve it in
                # this batch so a sibling can't grab it); a genuine host collision
                # surfaces at the docker bind rather than being silently moved.
                #
                # In TUNNEL mode (l4_bind_host == 127.0.0.1) this pinning is WRONG:
                # the public port is the relay's per-instance tunnel_port, and
                # port_host is just the loopback handle the rathole-client dials
                # (create_compose maps the container side to {{ instance_l4_port }},
                # so advertise == listen still holds). Pinning port_host to the
                # container port would make every tunnel instance of the same app
                # bind the SAME loopback port and the second one fail to start, so
                # tunnel L4 must fall through to normal per-instance allocation.
                if ports[i].get('same_port') and l4_bind_host != '127.0.0.1':
                    cport = int(ports[i]['port_container'])
                    assigned[i] = cport
                    used.add(cport)
                    continue
                prev = sticky.get(ports[i]['port_name'])
                if (prev is not None and prev not in used
                        and is_port_free(l4_bind_host, prev, proto)):
                    assigned[i] = prev
                    used.add(prev)
                else:
                    fresh_needed.append(i)
            if fresh_needed:
                fresh = allocate_ports_in_range(
                    l4_bind_host, len(fresh_needed), range_start, range_end,
                    protocol=proto, reserved=used)
                for i, host_port in zip(fresh_needed, fresh):
                    assigned[i] = host_port
                    used.add(host_port)
            for i in idxs:
                ports[i]['port_host'] = assigned[i]
                new_sticky[ports[i]['port_name']] = assigned[i]
        sticky_ports.save(greffon_path, instance_id, new_sticky)
    return greffon_info


def create_greffon_info(compose, greffon):
    greffon_path = os.path.join(
        os.getenv('GREFFON_PATH', '/data'), greffon['id'])
    internal_network_id = 'greffon_internal_network'
    nginx_volume_id = f'{greffon["id"]}_nginx_volume'
    services = list(compose['services'].keys())
    greffon_info = {
        'ports': [],
        'volumes': {},
        'id': greffon['id'],
        'configurations': greffon['configurations'],
        # Feature #4 (integrations): per-type config blobs forwarded
        # verbatim from the start request. compose.py's render pipeline
        # consumes this dict — it lifts each known integration type
        # (e.g. 'smtp') into the Jinja context as a top-level variable
        # AND deletes catalog-declared env keys for types the user
        # didn't pick. `.get('integrations', {})` keeps backwards-compat
        # with old manager versions whose payload omits the field.
        'integrations': greffon.get('integrations') or {},
        'networks': {
            internal_network_id: {
                'name': 'internal',
                'value': internal_network_id,
                'containers': services,
            },
        },
        'volumes': {
            'greffon_nginx': {
                'name': 'greffon_nginx',
                'value': nginx_volume_id,
                'containers': {
                    'greffon_nginx': {
                        'path': '/etc/nginx/'
                    }
                },
                'files': [
                    {
                        'type': 'path',
                        'src': os.path.join(greffon_path, 'nginx.conf'),
                        'dest': 'nginx.conf',
                    },
                    {
                        'type': 'content',
                        'content': greffon['cert']['certificate'],
                        'dest': 'pem.crt',
                    },
                    {
                        'type': 'content',
                        'content': greffon['cert']['private_key'],
                        'dest': 'cert.key',
                    },
                ]
            }
        },
        'internal_network': internal_network_id,
        'services': {service: {'value': service} for service in services},
    }
    greffon_info['services']['greffon_nginx'] = {
        'value': 'greffon_nginx'
    }
    for _, volume_name in enumerate(compose.get('volumes', [])):
        # Namespace each compose-declared named volume by instance ID so two
        # greffons (or two instances of the same greffon) that both declare
        # e.g. `db_data` don't collide on a shared docker volume.
        greffon_info['volumes'][volume_name] = {
            'name': volume_name,
            'value': f'{greffon["id"]}_{volume_name}',
            'containers': {},
            'files': []
        }
    for _, network_name in enumerate(compose.get('networks', [])):
        greffon_info['networks'][network_name] = {
            'name': network_name,
            'value': network_name,
            'containers': {},
            'files': []
        }
    for name, service in compose['services'].items():
        ports = service.get('ports', [])
        if type(ports) == list:
            for port in ports:
                port_splited = port.split(':')
                port_container, parsed_proto = _split_proto(port_splited[-1])
                port_name = f'{name}_{port_container}'
                manager_port = greffon.get('ports', {}).get(port_name, {})
                greffon_info['ports'].append({
                    'port_container': port_container,
                    'container_name': name,
                    'port_name': port_name,
                    'url': manager_port.get('url'),
                    # Tier/protocol: manager (catalog) is authoritative; fall
                    # back to parsing "<h>:<c>/udp" from the compose, then to
                    # the http/tcp defaults.
                    'protocol': manager_port.get('protocol') or parsed_proto or 'tcp',
                    'exposure_tier': manager_port.get('exposure_tier', 'http'),
                    # same_port: publish host P -> container P (not declared
                    # container port) so the app advertises exactly what it
                    # binds. Manager-declared (L4 only); default off.
                    'same_port': bool(manager_port.get('same_port', False)),
                })
        else:
            greffon_info['ports'].setdefault(name, {})
            _, raw_container = port.split(':')
            port_container, parsed_proto = _split_proto(raw_container)
            port_name = f'{name}_{port_container}'
            manager_port = greffon.get('ports', {}).get(port_name, {})
            greffon_info['ports'].append({
                'port_container': port_container,
                'container_name': name,
                'port_name': port_name,
                'url': manager_port.get('url'),
                'protocol': manager_port.get('protocol') or parsed_proto or 'tcp',
                'exposure_tier': manager_port.get('exposure_tier', 'http'),
                'same_port': bool(manager_port.get('same_port', False)),
            })
        volumes = service.get('volumes', [])
        if type(volumes) == list:
            for volume in service.get('volumes', []):
                volume_host, volume_container = volume.split(':')
                if volume_host not in greffon_info['volumes']:
                    # todo should handle multi containers
                    greffon_info['volumes'][volume_host] = {
                        'name': volume_host,
                        'value': volume_host,
                        'containers': {
                            name: {
                                'path': volume_container
                            }
                        },
                        'files': []
                    }
                else:
                    greffon_info['volumes'][volume_host]['containers'][name] = {
                        'path': volume_container
                    }
        else:
            volume_host, volume_container = volume.split(':')
            if volume_host not in greffon_info['volumes']:
                greffon_info['volumes'][volume_host] = {
                    'name': volume_host,
                    'value': volume_host,
                    'containers': {
                        name: {
                            'path': volume_container
                        }
                    },
                    'files': []
                }
            else:
                greffon_info['volumes'][volume_host]['containers'][name] = {
                    'path': volume_container
                }
        networks = service.get('networks', [])
        if type(networks) == list:
            for network in networks:
                greffon_info['networks'][network]['containers'].append(name)
        else:
            greffon_info['networks'][networks]['containers'].append(name)
    return greffon_info
