"""Tests for the v2 updater spawn module (apps/utils/docker/updater.py).

The docker SDK client is faked, so no real daemon. Focus: host-path discovery
off the greffer's own container record (compose dir -> /work, /data preserved by
mount Type), the docker-socket mount, the run args, and the fail-closed errors.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import docker
import pytest

from apps.utils.docker import updater

_BIND_APP = {"Type": "bind", "Source": "/host/greffer", "Destination": "/app"}
_VOL_DATA = {"Type": "volume", "Name": "greffer-data", "Source": "/var/lib/docker/x",
             "Destination": "/data"}


class _FakeContainers:
    def __init__(self, attrs, *, run_id="cid0123456789", run_exc=None):
        self._attrs = attrs
        self._run_id = run_id
        self._run_exc = run_exc
        self.run_calls: list = []

    def get(self, name):
        c = MagicMock()
        c.attrs = self._attrs
        return c

    def run(self, image, command, **kwargs):
        self.run_calls.append({"image": image, "command": command, **kwargs})
        if self._run_exc is not None:
            raise self._run_exc
        c = MagicMock()
        c.id = self._run_id
        return c


class _FakeClient:
    def __init__(self, containers):
        self.containers = containers


def _client(mounts, **kw):
    return _FakeClient(_FakeContainers({"Mounts": mounts}, **kw))


def _by_target(mounts):
    return {m["Target"]: m for m in mounts}


def test_spawn_happy_wires_mounts_env_and_command():
    client = _client([_BIND_APP, _VOL_DATA])
    cid = updater.spawn_updater(
        image="greffon/greffer-updater@sha256:" + "a" * 64,
        target_tag="0.3.6", manifest_url="https://x/m.json",
        greffer_id="g1", mode="tunnel", client=client)
    assert cid == "cid0123456789"
    call = client.containers.run_calls[0]
    assert call["command"] == ["python", "-m", "greffer_cli.updater", "0.3.6"]
    assert call["detach"] is True
    assert call["environment"]["GREFFER_VERSION_MANIFEST_URL"] == "https://x/m.json"
    assert call["environment"]["GREFFER_ID"] == "g1"
    assert call["environment"]["GREFFER_MODE"] == "tunnel"
    mounts = _by_target(call["mounts"])
    # compose dir host source -> /work
    assert mounts["/work"]["Source"] == "/host/greffer"
    assert mounts["/work"]["Type"] == "bind"
    # /data preserved as a NAMED VOLUME (source = volume name, not host path)
    assert mounts["/data"]["Source"] == "greffer-data"
    assert mounts["/data"]["Type"] == "volume"
    # docker socket
    assert mounts["/var/run/docker.sock"]["Source"] == "/var/run/docker.sock"


def test_spawn_bind_data_preserves_host_path():
    bind_data = {"Type": "bind", "Source": "/host/data", "Destination": "/data"}
    client = _client([_BIND_APP, bind_data])
    updater.spawn_updater(
        image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
        greffer_id="g1", mode="proxy", client=client)
    mounts = _by_target(client.containers.run_calls[0]["mounts"])
    assert mounts["/data"]["Source"] == "/host/data"
    assert mounts["/data"]["Type"] == "bind"


def test_spawn_custom_data_dest():
    custom = {"Type": "volume", "Name": "v", "Destination": "/srv/state"}
    client = _client([_BIND_APP, custom])
    updater.spawn_updater(
        image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
        greffer_id="g1", mode="proxy", data_dest="/srv/state", client=client)
    mounts = _by_target(client.containers.run_calls[0]["mounts"])
    assert mounts["/data"]["Source"] == "v"


def test_spawn_empty_image_refuses():
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(
            image="", target_tag="0.3.6", manifest_url="https://x",
            greffer_id="g1", mode="proxy", client=_client([_BIND_APP, _VOL_DATA]))


def test_spawn_missing_data_mount_refuses():
    client = _client([_BIND_APP])  # no /data
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(
            image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
            greffer_id="g1", mode="proxy", client=client)
    assert client.containers.run_calls == []  # never spawned


def test_spawn_missing_compose_mount_refuses():
    client = _client([_VOL_DATA])  # no /app
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(
            image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
            greffer_id="g1", mode="proxy", client=client)


def test_spawn_volume_without_source_refuses():
    bad = {"Type": "volume", "Destination": "/data"}  # no Name/Source
    client = _client([_BIND_APP, bad])
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(
            image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
            greffer_id="g1", mode="proxy", client=client)


def test_spawn_self_not_found_refuses():
    containers = _FakeContainers({"Mounts": [_BIND_APP, _VOL_DATA]})
    containers.get = MagicMock(side_effect=docker.errors.NotFound("nope"))
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(
            image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
            greffer_id="g1", mode="proxy", client=_FakeClient(containers))


def test_spawn_run_api_error_refuses():
    client = _client([_BIND_APP, _VOL_DATA],
                     run_exc=docker.errors.APIError("daemon boom"))
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(
            image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
            greffer_id="g1", mode="proxy", client=client)


def test_spawn_none_greffer_id_becomes_empty_env():
    client = _client([_BIND_APP, _VOL_DATA])
    updater.spawn_updater(
        image="img@sha256:x", target_tag="0.3.6", manifest_url="https://x",
        greffer_id=None, mode="proxy", client=client)
    assert client.containers.run_calls[0]["environment"]["GREFFER_ID"] == ""
