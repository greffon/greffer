import os
import subprocess
from uuid import uuid4
import logging
# LOGGER_NAME is hardcoded to 'greffer' in greffer/settings.py; the env
# var override is kept for parity with the FastAPI settings. Decoupled
# from django.conf so this module imports in both runtimes.
logger = logging.getLogger(os.getenv('LOGGER_NAME', 'greffer'))

def docker_is_volume_exist(volume):
    res = subprocess.run(['docker', 'volume', 'ls',
                          '--format', '"{{.Name}}"', '-f', f'name={volume["value"]}'], capture_output=True)
    return res != ""


def docker_create_volume(volume):
    subprocess.run(['docker', 'volume', 'create', volume['value']])


def list_instance_volumes(instance_id):
    """Names of every docker volume namespaced to an instance (the
    ``<id>_<vol>`` scheme). Used by decommission to prune + then VERIFY the
    teardown is complete. Docker's ``name=`` filter is an unanchored substring
    match, so post-filter on the ``<id>_`` prefix to avoid matching a volume
    that merely contains the id mid-name (a UUID makes that near-impossible, but
    be precise)."""
    res = subprocess.run(
        ['docker', 'volume', 'ls', '-q', '-f', f'name={instance_id}_'],
        capture_output=True, text=True)
    if res.returncode != 0:
        # An un-queryable docker must NOT read as "no volumes" -- that would let
        # the decommission completeness verify report a false clean (the very
        # false-success the verify exists to prevent). Fail loud so the caller
        # treats it as not-verified.
        raise RuntimeError(
            f'docker volume ls failed (rc={res.returncode}): {res.stderr.strip()}')
    prefix = f'{instance_id}_'
    return [line for line in res.stdout.splitlines()
            if line.strip().startswith(prefix)]


def remove_instance_volumes(instance_id):
    """Force-remove every ``<id>_``-prefixed volume; return the names removed.
    Best-effort per volume (an in-use volume cannot be removed -- the caller
    re-lists afterwards to confirm nothing remains)."""
    names = list_instance_volumes(instance_id)
    for name in names:
        result = subprocess.run(['docker', 'volume', 'rm', '-f', name],
                                capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning('volume_rm_failed volume=%s err=%s',
                           name, result.stderr.strip())
    return names


def docker_copy_file_into_volume(volume):
    #@Todo should handle error
    container_name = str(uuid4())
    subprocess.run(['docker',  'container', 'create', '--name',
                   container_name, '-v', f'{volume["value"]}:/root', 'hello-world'])
    for file in volume.get('files', []):
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
