import os
import socket
import threading
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

REQUEST_TIMEOUT = 30

# Set by register() once cert material is on disk. monitor_status (and any
# other late-start consumer) waits on this before calling the manager so
# bootstrap-window calls don't race the registration flow.
_registered = threading.Event()


def wait_for_registration(timeout=None):
    return _registered.wait(timeout)


def _write_local_cert(file_name, content, mode=0o644):
    """Write atomically: tmp file with explicit mode, then os.rename. Prevents
    other threads from reading a truncated or half-written PEM."""
    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)
    path = f'{CERT_DIR}/{file_name}'
    tmp = f'{path}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    os.rename(tmp, path)


def _client_auth():
    """Return requests kwargs. Present the greffer's client cert whenever
    cert+key are on disk (post-registration); CA presence is an independent
    verify-override since ``issuing_ca`` is optional in the cert response.
    Falls back to system-CA verification when the manager didn't (or couldn't)
    ship its CA bundle.

    register() writes key before cert, so ``CERT_PATH exists`` implies
    ``KEY_PATH exists`` — no half-written pair can ever be loaded."""
    kwargs = {'verify': True}
    if os.path.exists(CA_PATH):
        kwargs['verify'] = CA_PATH
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        kwargs['cert'] = (CERT_PATH, KEY_PATH)
    return kwargs


def _check_secure_bootstrap():
    """Registration carries the greffer token and receives the signed private
    key in the response body. Refuse to proceed if the channel isn't https,
    unless the operator opted in explicitly — dev stacks that terminate TLS
    elsewhere can set GREFFER_ALLOW_INSECURE_BOOTSTRAP=1."""
    if base_server.startswith('https://'):
        return
    if os.getenv('GREFFER_ALLOW_INSECURE_BOOTSTRAP', '').lower() in ('1', 'true', 't', 'yes'):
        logger.warning(
            f'GREFFON_BASE_SERVER={base_server!r} is insecure — token and '
            f'private key will be sent in cleartext. This must be https:// '
            f'in production.'
        )
        return
    raise RuntimeError(
        f'GREFFON_BASE_SERVER={base_server!r} is not https. The bootstrap '
        f'register/cert-poll calls carry the greffer token and receive the '
        f'greffer private key in the response body. Set '
        f'GREFFER_ALLOW_INSECURE_BOOTSTRAP=1 to permit (dev only).'
    )


def register():
    _check_secure_bootstrap()
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
                timeout=REQUEST_TIMEOUT,
                **_client_auth(),
            )
            break
        except requests.RequestException as e:
            logger.warning(f'Registration POST failed, retrying in 3s: {e}')
            time.sleep(3)

    while True:
        try:
            res = requests.get(
                f'{base_server}/api/greffer/certificate/{greffer_id}/',
                timeout=REQUEST_TIMEOUT,
                **_client_auth(),
            )
        except requests.RequestException as e:
            logger.warning(f'Cert fetch failed, retrying in 5s: {e}')
            time.sleep(5)
            continue
        if res.status_code != 200:
            time.sleep(5)
            continue
        try:
            data = res.json()
            certificate = data['certificate']
            private_key = data['private_key']
        except (ValueError, KeyError) as e:
            logger.error(f'Malformed cert response: {e}; body={res.text!r}')
            time.sleep(5)
            continue
        # Write key before cert so _client_auth's "cert exists" check implies
        # key is already fully durable; every write is atomic (tmp+rename) so
        # no partial state is ever readable.
        _write_local_cert('cert.key', private_key, mode=0o600)
        _write_local_cert('pem.crt', certificate)
        if 'issuing_ca' in data:
            _write_local_cert('ca.pem', data['issuing_ca'])
        copy_file_into_container(docker_nginx_name, '/root', 'pem.crt', certificate)
        copy_file_into_container(docker_nginx_name, '/root', 'cert.key', private_key)
        if 'issuing_ca' in data:
            copy_file_into_container(docker_nginx_name, '/root', 'ca.pem', data['issuing_ca'])
        _registered.set()
        break


def change_status(greffon_id, status):
    try:
        return requests.post(
            f'{base_server}/api/greffer/instances/{greffon_id}/',
            json={
                'status': status,
            },
            timeout=REQUEST_TIMEOUT,
            **_client_auth(),
        )
    except requests.RequestException as e:
        logger.error(f'status_update_failed greffon_id={greffon_id} error={e}')
        return None
