import requests
import os
import yaml
from apps.utils.os.network import get_free_ports

def get_compose_file_from_repository(greffon):
  r = requests.get(greffon['repository_url'])
  return yaml.safe_load(r.text)


def get_greffon_info(compose, greffon):
    greffon_info = create_greffon_info(compose, greffon)
    ports = get_free_ports(numbers=len(greffon_info['ports']))
    for i, port in enumerate(ports):
        greffon_info['ports'][i]['port_host'] = port
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
        greffon_info['volumes'][volume_name] = {
            'name': volume_name,
            'value': volume_name,
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
                port_name =  f'{name}_{port_splited[-1]}'
                greffon_info['ports'].append({
                    'port_container': port_splited[-1],
                    'container_name': name,
                    'port_name': port_name,
                    'url': greffon.get('ports', {}).get(port_name, {}).get('url'),
                })
        else:
            greffon_info['ports'].setdefault(name, {})
            _, port_container = port.split(':')
            port_name = f'{name}_{port_container}'
            greffon_info['ports'].append({
                'port_container': port_container,
                'container_name': name,
                'port_name': port_name,
                'url': greffon.get('ports', {}).get(port_name, {}).get('url'),
            })
        volumes = service.get('volumes', [])
        if type(volumes) == list:
            for volume in service.get('volumes', []):
                volume_host, volume_container = volume.split(':')
                if volume_host not in greffon_info['volumes']:
                    #todo should handle multi containers
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
        networks = service.get('networks', [])
        if type(networks) == list:
            for network in networks:
                greffon_info['networks'][network]['containers'].append(name)
        else:
            greffon_info['networks'][networks]['containers'].append(name)
    return greffon_info
