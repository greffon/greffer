import os
import socket
import time

import requests

from apps.utils.auth import get_token
from apps.utils.docker.base import copy_file_into_container

import logging
from django.conf import settings

logger = logging.getLogger(settings.LOGGER_NAME)

base_server = os.getenv('GREFFON_BASE_SERVER', 'https://api.greffon.io')
docker_nginx_name = os.getenv('DOCKER_NGINX_NAME', 'greffer-nginx-1')
greffer_protocol = os.getenv('GREFFER_PROTOCOL')

# Local cert material used by this process for mTLS outbound to the manager.
# Nginx keeps its own copy at /root/ (for TLS termination on inbound) via
# copy_file_into_container below.
CERT_DIR = '/etc/greffer/certs'
CERT_PATH = f'{CERT_DIR}/pem.crt'
KEY_PATH = f'{CERT_DIR}/cert.key'
CA_PATH = f'{CERT_DIR}/ca.pem'

CRL_SYNC_INTERVAL = int(os.getenv('CRL_SYNC_INTERVAL', '300'))


def _write_local_cert(file_name, content, mode=0o644):
    os.makedirs(CERT_DIR, exist_ok=True)
    path = f'{CERT_DIR}/{file_name}'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


def _client_auth():
    """Return requests kwargs. Once cert material is on disk, calls present the
    greffer's client cert and verify the manager against the CA it issued us.
    Before registration (bootstrap window) we have no cert yet, so fall back to
    system-CA verification."""
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH) and os.path.exists(CA_PATH):
        return {'verify': CA_PATH, 'cert': (CERT_PATH, KEY_PATH)}
    return {'verify': True}


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
                **_client_auth(),
            )
            break
        except requests.ConnectionError:
            logger.info(f'Manager not reachable at {base_server}, retrying in 3s...')
            time.sleep(3)

    while True:
        res = requests.get(
            f'{base_server}/api/greffer/certificate/{greffer_id}/',
            **_client_auth(),
        )
        if res.status_code == 200:
            data = res.json()
            _write_local_cert('pem.crt', data['certificate'])
            _write_local_cert('cert.key', data['private_key'], mode=0o600)
            if 'issuing_ca' in data:
                _write_local_cert('ca.pem', data['issuing_ca'])
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
        res = requests.get(
            f'{base_server}/api/greffer/ca/crl/',
            timeout=10,
            **_client_auth(),
        )
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
        **_client_auth(),
    )
