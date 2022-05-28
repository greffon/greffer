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

def register():
    base_server = os.getenv('GREFFON_BASE_SERVER', 'https://greffon.io')
    greffer_url = os.getenv('GREFFER_ADDRESS')
    greffer_port = os.getenv('GREFFER_PORT')
    greffer_id = os.getenv('GREFFER_ID')
    greffer_protocol = os.getenv('GREFFER_PROTOCOL')
    if not greffer_url:
        hostname = socket.gethostname()
        greffer_url = socket.gethostbyname(hostname)
    requests.post(f'{base_server}/api/greffer/register/{greffer_id}/',json={
        'address': greffer_url,
        'port': greffer_port,
        'token': get_token(),
        'protocol': greffer_protocol,
    })
    while True:
        res = requests.get(f'{base_server}/api/greffer/certificate/{greffer_id}/')
        if res.status_code == 200:
            data = res.json()
            #Todo: use right docker id
            copy_file_into_container('greffer_nginx_1', '/root','pem.crt', data['certificate'])
            copy_file_into_container('greffer_nginx_1', '/root', 'cert.key', data['private_key'])
            break
        time.sleep(5)
