import subprocess
from uuid import uuid4
import logging
from django.conf import settings
logger = logging.getLogger(settings.LOGGER_NAME)

def docker_is_volume_exist(volume):
    res = subprocess.run(['docker', 'volume', 'ls',
                          '--format', '"{{.Name}}"', '-f', f'name={volume["value"]}'], capture_output=True)
    return res != ""


def docker_create_volume(volume):
    subprocess.run(['docker', 'volume', 'create', volume['value']])


def docker_copy_file_into_volume(volume):
    #@Todo should handle error
    container_name = str(uuid4())
    subprocess.run(['docker',  'container', 'create', '--name',
                   container_name, '-v', f'{volume["value"]}:/root', 'hello-world'])
    for file in volume.get('files', []):
        logger.info(file)
        file_src = None
        if file['type'] == 'path':
            file_src = file['src']
        elif file['type'] == 'content':
            file_src = str(uuid4())
            with open(file_src, "xt") as f:
                f.write(file['content'])
                f.close()
        subprocess.run(['docker', 'cp', file_src,
                       f'{container_name}:/root/{file["dest"]}'])

    subprocess.run(['docker', 'rm', container_name])
