import subprocess
from uuid import uuid4

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
        subprocess.run(['docker', 'cp', file['src'],
                       f'{container_name}:/root/{file["dest"]}'])

    subprocess.run(['docker', 'rm', container_name])
