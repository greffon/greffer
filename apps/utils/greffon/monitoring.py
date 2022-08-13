import time
import os
from apps.utils.docker import compose
from apps.utils.greffon import base_server
import logging
from django.conf import settings
logger = logging.getLogger(settings.LOGGER_NAME)

def monitor_status(delay=5):
    greffon_dir = os.getenv('GREFFON_PATH')
    prev_status = {}
    while True:
        logger.info("monitoring begin")
        for greffon_id in os.listdir(greffon_dir):
            status = compose.get_status(greffon_id)['status']
            if prev_status.get(greffon_id) != status:
                base_server.change_status(greffon_id, status)
            prev_status[greffon_id] = status
        time.sleep(delay)