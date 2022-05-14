import docker
import tarfile
import time
from io import BytesIO

client = docker.from_env()

def copy_file_into_container(container_name, path, file_name, file_content):
    data = file_content.encode('utf-8')
    tarstream = BytesIO()
    tar = tarfile.TarFile(fileobj=tarstream, mode='w')
    tarinfo = tarfile.TarInfo(name=file_name)
    tarinfo.size = len(data)
    tarinfo.mtime = time.time()
    tar.addfile(tarinfo, BytesIO(data))
    tar.close()

    tarstream.seek(0)
    #@Todo should handle error
    container = client.containers.get(container_name)
    container.put_archive(path, tarstream)