import requests
import time
import os
import socket

from apps.utils.docker.base import copy_file_into_container
from apps.utils.auth import get_token

import logging
from django.conf import settings

# Get an instance of a logger
logger = logging.getLogger(settings.LOGGER_NAME)

base_server = os.getenv('GREFFON_BASE_SERVER', 'https://api.greffon.io')
docker_nginx_name = os.getenv('DOCKER_NGINX_NAME', 'greffer-nginx-1')
greffer_protocol = os.getenv('GREFFER_PROTOCOL')
ssl_verify = os.getenv("GREFFER_SSL_VERIFY", 'true').lower() in ('true', '1', 't')

def register():
    greffer_url = os.getenv('GREFFER_ADDRESS')
    greffer_port = os.getenv('GREFFER_PORT')
    greffer_id = os.getenv('GREFFER_ID')
    if not greffer_url:
        hostname = socket.gethostname()
        greffer_url = socket.gethostbyname(hostname)
    # Retry registration until the manager is reachable
    while True:
        try:
            requests.post(f'{base_server}/api/greffer/register/{greffer_id}/',json={
                'address': greffer_url,
                'port': greffer_port,
                'token': get_token(),
                'protocol': greffer_protocol,
            }, verify=ssl_verify)
            break
        except requests.ConnectionError:
            logger.info(f'Manager not reachable at {base_server}, retrying in 3s...')
            time.sleep(3)
    #Todo Maybe we should expose api
    while True:
        res = requests.get(f'{base_server}/api/greffer/certificate/{greffer_id}/', verify=ssl_verify)
        if res.status_code == 200:
            data = res.json()
            #Todo: use right docker id
            copy_file_into_container(docker_nginx_name, '/root','pem.crt', data['certificate'])
            copy_file_into_container(docker_nginx_name, '/root', 'cert.key', data['private_key'])
            break
        time.sleep(5)

def change_status(greffon_id, status):
    return requests.post(f'{base_server}/api/greffer/instances/{greffon_id}/',json={
        'status': status,
    }, verify=ssl_verify)
