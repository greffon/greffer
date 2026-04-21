import os
import socket
import time

import requests

from apps.utils.auth import get_token
from apps.utils.docker.base import copy_file_into_container

import logging

# LOGGER_NAME is hardcoded to 'greffer' in greffer/settings.py; the env
# var override is kept for parity with the FastAPI settings. Decoupled
# from django.conf so this module imports in both runtimes.
logger = logging.getLogger(os.getenv('LOGGER_NAME', 'greffer'))

base_server = os.getenv('GREFFON_BASE_SERVER', 'https://api.greffon.io')
docker_nginx_name = os.getenv('DOCKER_NGINX_NAME', 'greffer-nginx-1')
greffer_protocol = os.getenv('GREFFER_PROTOCOL')
ssl_verify = os.getenv('GREFFER_SSL_VERIFY', 'true').lower() in ('true', '1', 't')

CRL_SYNC_INTERVAL = int(os.getenv('CRL_SYNC_INTERVAL', '300'))


def register():
    greffer_url = os.getenv('GREFFER_ADDRESS')
    greffer_port = os.getenv('GREFFER_PORT')
    greffer_id = os.getenv('GREFFER_ID')
    if not greffer_url:
        hostname = socket.gethostname()
        greffer_url = socket.gethostbyname(hostname)
    while True:
        try:
            requests.post(
                f'{base_server}/api/greffer/register/{greffer_id}/',
                json={
                    'address': greffer_url,
                    'port': greffer_port,
                    'token': get_token(),
                    'protocol': greffer_protocol,
                },
                verify=ssl_verify,
            )
            break
        except requests.ConnectionError:
            logger.info(f'Manager not reachable at {base_server}, retrying in 3s...')
            time.sleep(3)

    while True:
        res = requests.get(f'{base_server}/api/greffer/certificate/{greffer_id}/', verify=ssl_verify)
        if res.status_code == 200:
            data = res.json()
            copy_file_into_container(docker_nginx_name, '/root', 'pem.crt', data['certificate'])
            copy_file_into_container(docker_nginx_name, '/root', 'cert.key', data['private_key'])
            if 'issuing_ca' in data:
                copy_file_into_container(docker_nginx_name, '/root', 'ca.pem', data['issuing_ca'])
            _fetch_and_store_crl()
            break
        time.sleep(5)


def _fetch_and_store_crl():
    """Fetch CRL from manager and copy into nginx container.
    The greffer nginx's inotifywait loop auto-reloads when files change in /root/.
    """
    try:
        res = requests.get(f'{base_server}/api/greffer/ca/crl/', verify=ssl_verify, timeout=10)
        if res.status_code == 200:
            copy_file_into_container(docker_nginx_name, '/root', 'revoked.crl', res.text)
            logger.info('CRL updated successfully')
        else:
            logger.warning(f'Failed to fetch CRL: HTTP {res.status_code}')
    except Exception as e:
        logger.warning(f'Failed to fetch CRL: {e}')


def sync_crl():
    while True:
        time.sleep(CRL_SYNC_INTERVAL)
        _fetch_and_store_crl()


def change_status(greffon_id, status):
    return requests.post(
        f'{base_server}/api/greffer/instances/{greffon_id}/',
        json={
            'status': status,
        },
        verify=ssl_verify,
    )
