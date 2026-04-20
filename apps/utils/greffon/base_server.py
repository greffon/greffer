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


def _write_local_cert(file_name, content, mode=0o644):
    os.makedirs(CERT_DIR, exist_ok=True)
    path = f'{CERT_DIR}/{file_name}'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


def _client_auth():
    """Return requests kwargs. Present the greffer's client cert whenever
    cert+key are on disk (post-registration); CA presence is an independent
    verify-override since ``issuing_ca`` is optional in the cert response.
    Falls back to system-CA verification when the manager didn't (or couldn't)
    ship its CA bundle."""
    kwargs = {'verify': True}
    if os.path.exists(CA_PATH):
        kwargs['verify'] = CA_PATH
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        kwargs['cert'] = (CERT_PATH, KEY_PATH)
    return kwargs


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
            break
        time.sleep(5)


def change_status(greffon_id, status):
    return requests.post(
        f'{base_server}/api/greffer/instances/{greffon_id}/',
        json={
            'status': status,
        },
        **_client_auth(),
    )
