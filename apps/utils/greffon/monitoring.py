import time
import os
from apps.utils.docker import compose
from apps.utils.greffon import base_server
import logging
# LOGGER_NAME is hardcoded to 'greffer' in greffer/settings.py; the env
# var override is kept for parity with the FastAPI settings. Decoupled
# from django.conf so this module imports in both runtimes.
logger = logging.getLogger(os.getenv('LOGGER_NAME', 'greffer'))


def monitor_status(delay=5):
    greffon_dir = os.getenv('GREFFON_PATH')
    prev_status = {}
    try:
        while True:
            logger.info("monitoring begin")
            for greffon_id in os.listdir(greffon_dir):
                status = compose.get_status(greffon_id)['status']
                if prev_status.get(greffon_id) != status:
                    base_server.change_status(greffon_id, status)
                prev_status[greffon_id] = status
            time.sleep(delay)
    except Exception as e:
        logger.error(e)
        time.sleep(delay*2)
