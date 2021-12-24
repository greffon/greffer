import requests
import os
import socket

from apps.utils.auth import get_token

import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)
def register():
    base_server = os.getenv('GREFFON_BASE_SERVER', 'https://greffon.io')
    greffer_url = os.getenv('GREFFER_ADDRESS')
    greffer_port = os.getenv('GREFFER_PORT')
    greffer_id = os.getenv('GREFFER_ID')
    if not greffer_url:
        hostname = socket.gethostname()
        greffer_url = socket.gethostbyname(hostname)
    requests.post(f'{base_server}/api/greffer/register/{greffer_id}/',json={
        'address': greffer_url,
        'port': greffer_port,
        'token': get_token(),
        'protocol': 'http'
    })
