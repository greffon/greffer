import requests
import os
import yaml
from apps.utils.os.network import get_free_ports

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


def get_greffon_info(compose, greffon):
    greffon_info = create_greffon_info(compose, greffon)
    # Allocate a free host port per declared port, batched by protocol so TCP
    # and UDP are probed in their own namespace and no number is handed out
    # twice within a protocol.
    ports = greffon_info['ports']
    idx_by_proto = {}
    for i, port in enumerate(ports):
        idx_by_proto.setdefault(port.get('protocol', 'tcp'), []).append(i)
    for proto, idxs in idx_by_proto.items():
        free = get_free_ports(numbers=len(idxs), protocol=proto)
        for idx, host_port in zip(idxs, free):
            ports[idx]['port_host'] = host_port
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
