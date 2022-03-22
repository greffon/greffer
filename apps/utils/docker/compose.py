import yaml
import json
from datauri import DataURI
from jinja2 import Template
import docker
import subprocess
import os

client = docker.from_env()

from apps.utils.docker.volume import docker_copy_file_into_volume, docker_create_volume, docker_is_volume_exist

def get_nginx_service(greffon):
    #@Todo should handle conflict port
    return {
        'image': 'nginx:1.20.2-alpine-perl',
        'restart': 'unless-stopped',
        'ports': [ ('{{ports[%s].port_host}}:%s' % (i, port['port_container'])) for i, port in enumerate(greffon['ports'])],
        'networks': [greffon['internal_network']],
    }



def create_compose_template_from_greffon(compose, greffon_info):
    for service_name, service in compose['services'].items():
        service['ports'] = []
        service['volumes'] = []
        service['networks'] = []
        if 'container_name' in service:
            del service['container_name']
    compose['services']['greffon_nginx'] = get_nginx_service(greffon_info)
    for _,volume in greffon_info['volumes'].items():
        for container_name, container in volume['containers'].items():
            compose['services'][container_name].setdefault('volumes', [])
            compose['services'][container_name]['volumes'].append(f'{volume["value"]}:{container["path"]}')
    for _,network in greffon_info['networks'].items():
        for _, container_name in enumerate(network['containers']):
            compose['services'][container_name]['networks'].append(network['value'])
    
    
    compose['volumes'] = { volume['value']: {'name': volume['value'] } for _,volume in greffon_info['volumes'].items()}
    compose['networks'] = { network['value']: {} for _,network in greffon_info['networks'].items()}
    compose['services'] = { greffon_info['services'][service_name]['value']: service for service_name, service in compose['services'].items() }
    return compose


def get_compose_template(compose, greffon_info):
    compose = create_compose_template_from_greffon(compose, greffon_info)
    return compose


def get_greffon_path(greffon_info):
    path = os.path.join(os.getenv('GREFFON_PATH', '/data'), greffon_info['id'])
    isExist = os.path.exists(path)
    if not isExist: 
        os.makedirs(path)
    return path

def create_compose(compose, greffon_info):
    greffon_path = os.path.join(os.getenv('GREFFON_PATH', '/data'), greffon_info['id'])
    if not os.path.exists(greffon_path):
        os.makedirs(greffon_path)
    t = Template(yaml.dump(compose))
    compose_file = t.render(**greffon_info)
    with open(os.path.join(greffon_path, 'docker-compose.yml'), 'w') as temp_file:
        temp_file.write(compose_file)

def remove_compose_file(greffon_info):
    greffon_path = get_greffon_path(greffon_info)
    template_path = os.path.join(greffon_path, 'docker-compose.template.yml')
    compose_path = os.path.join(greffon_path, 'docker-compose.yml')
    if os.path.exists(template_path):
        os.remove(template_path)
    if os.path.exists(compose_path):
        os.remove(compose_path)

def create_volumes_then_copy_files(greffon_info):
    for _, volume in greffon_info.get('volumes', {}).items():
        if not docker_is_volume_exist(volume):
            docker_create_volume(volume)
        docker_copy_file_into_volume(volume)

def apply_configuration(greffon_info, compose):
    for configuration in greffon_info.get('configurations', []):
        for destination in configuration.get('destinations', []):
            if destination['type'] == 'json':
                file_path = os.path.join(get_greffon_path(greffon_info), destination['name'])
                with open(file_path, "w") as f:   # Opens file and casts as f 
                    f.write(json.dumps(configuration['value']))
                greffon_info['volumes'][destination['volume']]['files'].append({
                            'src': file_path,
                            'dest': destination['name'],
                        })
            elif destination['type'] == 'env':
                remove_compose_file(greffon_info)
                compose['services'][destination['container']].setdefault('environment', [])
                compose['services'][destination['container']]['environment'].append(f'{destination["key"]}={configuration["value"]["value"]}')
            elif destination['type'] == 'file':
                remove_compose_file(greffon_info)
                file_path = os.path.join(get_greffon_path(greffon_info), destination['name'])
                uri = DataURI(configuration['value']['file'])
                with open(file_path, "wb") as f:   # Opens file and casts as f 
                    f.write(uri.data)
                greffon_info['volumes'][destination['volume']]['files'].append({
                            'src': file_path,
                            'dest': destination['name'],
                        })
    return greffon_info


def start(greffon_info):
    return subprocess.Popen(['docker-compose', '-f', os.path.join(get_greffon_path(greffon_info), 
    'docker-compose.yml'), 'up'])

def stop(greffon_info):
    return subprocess.Popen(['docker-compose', '-f', os.path.join(get_greffon_path(greffon_info), 
    'docker-compose.yml'), 'stop'])


def status(greffon_id): 
    containers = []
    compose_status = 'running'
    for container in client.containers.list(all=True, filter=f'{greffon_id}_*'):
        container_status = container.status
        if container_status != 'running': 
            container_status = 'stopped'
        containers.append({
            'status': container.status
        })
    return {
        'status': compose_status,
        containers: containers
    }