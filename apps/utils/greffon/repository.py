import logging
import requests
import os
import yaml
from apps.utils.os.network import get_free_ports
from apps.utils.greffon import sticky_ports
from apps.utils.docker import l4_ports

logger = logging.getLogger("greffer")

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


# Catalog authors set this label on a service whose app streams responses
# (Server-Sent Events, long-poll, chunked) so the generated nginx proxy stops
# buffering them. Without it nginx's default response buffering holds the
# stream and the app's live updates never reach the browser. The nginx
# template also emits `X-Accel-Buffering: no` for these ports so any *outer*
# proxy (the manager edge nginx) disables buffering for the response too,
# without needing per-greffon knowledge of its own.
PROXY_STREAMING_LABEL = 'com.greffon.proxy.streaming'
_STREAMING_TRUTHY = {'true', '1', 'yes', 'on'}


def _service_streaming(service):
    """Read the streaming label off a parsed compose service.

    Compose accepts labels as a mapping (`key: value`) or a list of
    `key=value` strings; handle both, and tolerate a malformed labels block
    (a bare string, a non-string list item) by treating it as "no label"
    rather than crashing the whole start flow. Returns True only for an
    explicit truthy value so the default stays buffered for every other
    greffon. Truthy spellings mirror docker/YAML (`true`, `1`, `yes`, `on`);
    YAML also pre-coerces bare `true`/`yes`/`on` to a bool, which str()s back
    into this set.
    """
    labels = (service or {}).get('labels', {}) or {}
    if isinstance(labels, list):
        labels = dict(
            item.split('=', 1) if '=' in item else (item, '')
            for item in labels if isinstance(item, str)
        )
    elif not isinstance(labels, dict):
        return False
    return str(labels.get(PROXY_STREAMING_LABEL, '')).strip().lower() in _STREAMING_TRUTHY


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

    # L4 (Tier-C) ports: STICKY + cross-instance, decided against the docker
    # daemon. The host:port IS the user-facing endpoint (baked into client
    # configs and persisted inside the app), so reuse the previously-allocated
    # port when it is still free; otherwise take the lowest free one from the
    # dedicated L4 range. "Free" is what the daemon publishes for RUNNING
    # containers, NOT a socket.bind probe: the greffer runs in its own container
    # network namespace and is blind to host bindings, so a probe reads a
    # host-occupied port as free and hands the same number to two instances.
    # See apps/utils/docker/l4_ports.py.
    l4 = [i for i, p in enumerate(ports) if p.get('exposure_tier') == 'l4']
    if l4:
        range_start = int(os.getenv('GREFFER_L4_PORT_RANGE_START', '20000'))
        range_end = int(os.getenv('GREFFER_L4_PORT_RANGE_END', '29999'))
        sticky = sticky_ports.load(greffon_path, instance_id)
        is_tunnel = l4_bind_host == '127.0.0.1'
        # Serialise the enumerate -> pick -> reserve decision across concurrent
        # in-process starts (FastAPI runs sync handlers in a threadpool, so two
        # different instances starting at once are real threads). The lock is
        # released before docker-compose up (the compose run is NOT serialised);
        # the pending set bridges the gap until the chosen port's container is
        # daemon-visible. See apps/utils/docker/l4_ports.py.
        with l4_ports.allocation_lock():
            # Ports held right now by every OTHER instance on this host, per
            # protocol. Excludes this instance's own compose project so a
            # re-deploy keeps its current ports. Raises L4PortsUnavailable
            # (-> a clean 503 in the controller) rather than degrading to
            # "nothing reserved".
            occupied = l4_ports.published_l4_ports(
                range_start, range_end, exclude_project=instance_id)
            pending = l4_ports.pending_and_prune(
                occupied, exclude_instance=instance_id)
            batch = {}        # proto -> host ports already assigned this call
            new_sticky = {}
            for i in l4:
                proto = ports[i].get('protocol', 'tcp')
                name = ports[i]['port_name']
                prev = sticky.get(name)
                taken = (occupied.get(proto, set()) | pending.get(proto, set())
                         | batch.get(proto, set()))
                if prev is not None and prev not in taken:
                    host_port = prev                  # sticky reuse: still free
                elif bool(ports[i].get('same_port')) and not is_tunnel \
                        and prev is not None:
                    # Proxy same_port: the pinned advertised port is taken and
                    # must NOT be rotated (clients baked it in). Fail loudly.
                    raise l4_ports.L4SamePortConflict(port_name=name, port=prev)
                else:
                    host_port = l4_ports.lowest_free_port(
                        range_start, range_end, taken)
                    if host_port is None:
                        raise l4_ports.L4PortRangeExhausted(
                            range_start, range_end)
                    if prev is not None and prev != host_port:
                        logger.info(
                            "l4_port_rotation instance_id=%s port_name=%s "
                            "old=%s new=%s", instance_id, name, prev, host_port)
                l4_ports.mark_pending(instance_id, proto, host_port)
                batch.setdefault(proto, set()).add(host_port)
                ports[i]['port_host'] = host_port
                new_sticky[name] = host_port
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
                    'streaming': _service_streaming(service),
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
                'streaming': _service_streaming(service),
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
