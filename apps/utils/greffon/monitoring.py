import time
import os
from apps.utils.docker import compose
from apps.utils.greffon import base_server
import logging
from django.conf import settings
logger = logging.getLogger(settings.LOGGER_NAME)


def monitor_status(delay=5):
    # Block until register() has cert material on disk. Otherwise status
    # callbacks fire in the bootstrap posture (no client cert) and get
    # rejected once manager-side mTLS enforcement is live.
    base_server.wait_for_registration()
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
