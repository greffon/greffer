"""Tests for the v2 updater spawn module (apps/utils/docker/updater.py).

The docker SDK client is faked, so no real daemon. Focus: socket-only wiring
(/data preserved by mount Type + the docker socket, NO compose dir), target_tag
passed via env, the run args, the digest-pin contract, the fail-closed errors,
and the update_in_progress lock probe (HLD section 10).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import docker
import pytest

from apps.utils.docker import updater

_IMG = "greffon/greffer-updater@sha256:" + "0" * 64
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


def test_spawn_happy_socket_only_with_target_tag_env():
    client = _client([_VOL_DATA])
    cid = updater.spawn_updater(
        image="greffon/greffer-updater@sha256:" + "a" * 64,
        target_tag="0.3.6", greffer_id="g1", client=client)
    assert cid == "cid0123456789"
    call = client.containers.run_calls[0]
    # no positional target_tag arg (it goes via env now)
    assert call["command"] == ["python", "-m", "greffer_cli.updater"]
    assert call["detach"] is True
    assert call["remove"] is True  # one-shot: don't litter the host
    assert call["environment"]["GREFFER_ID"] == "g1"
    assert call["environment"]["GREFFER_UPDATER_TARGET_TAG"] == "0.3.6"
    # the socket model drops the manifest + mode env entirely
    assert "GREFFER_VERSION_MANIFEST_URL" not in call["environment"]
    assert "GREFFER_MODE" not in call["environment"]
    mounts = _by_target(call["mounts"])
    # socket-only: ONLY /data + the docker socket, NO /work compose dir
    assert set(mounts) == {"/data", "/var/run/docker.sock"}
    assert mounts["/data"]["Source"] == "greffer-data"  # named volume preserved
    assert mounts["/data"]["Type"] == "volume"
    assert mounts["/var/run/docker.sock"]["Type"] == "bind"


def test_spawn_no_target_tag_omits_env():
    # latest (None) -> no GREFFER_UPDATER_TARGET_TAG; the updater defaults to latest
    client = _client([_VOL_DATA])
    updater.spawn_updater(image=_IMG, target_tag=None, greffer_id="g1", client=client)
    assert "GREFFER_UPDATER_TARGET_TAG" not in client.containers.run_calls[0]["environment"]


def test_spawn_unpinned_image_refuses():
    client = _client([_VOL_DATA])
    for bad in ("greffon/greffer-updater:latest", "greffon/greffer-updater",
                "greffon/greffer-updater@sha256:tooshort"):
        with pytest.raises(updater.UpdaterSpawnError):
            updater.spawn_updater(image=bad, target_tag="0.3.6", greffer_id="g1",
                                  client=client)
    assert client.containers.run_calls == []  # never spawned


def test_is_digest_pinned():
    assert updater.is_digest_pinned("r@sha256:" + "a" * 64) is True
    assert updater.is_digest_pinned("greffon/greffer-updater@sha256:" + "0" * 64) is True
    assert updater.is_digest_pinned("r:latest") is False
    assert updater.is_digest_pinned("r@sha256:" + "a" * 63) is False
    assert updater.is_digest_pinned("r@sha256:" + "g" * 64) is False
    assert updater.is_digest_pinned(None) is False
    # fullmatch: a double-digest or trailing junk cannot slip past
    assert updater.is_digest_pinned(
        "a@sha256:" + "a" * 64 + "@sha256:" + "b" * 64) is False
    assert updater.is_digest_pinned("r@sha256:" + "a" * 64 + " evil") is False


def test_spawn_bind_data_preserves_host_path():
    bind_data = {"Type": "bind", "Source": "/host/data", "Destination": "/data"}
    client = _client([bind_data])
    updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id="g1", client=client)
    mounts = _by_target(client.containers.run_calls[0]["mounts"])
    assert mounts["/data"]["Source"] == "/host/data"
    assert mounts["/data"]["Type"] == "bind"


def test_spawn_custom_data_dest():
    custom = {"Type": "volume", "Name": "v", "Destination": "/srv/state"}
    client = _client([custom])
    updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id="g1",
                          data_dest="/srv/state", client=client)
    assert _by_target(client.containers.run_calls[0]["mounts"])["/data"]["Source"] == "v"


def test_spawn_empty_image_refuses():
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(image="", target_tag="0.3.6", greffer_id="g1",
                              client=_client([_VOL_DATA]))


def test_spawn_missing_data_mount_refuses():
    client = _client([])  # no /data
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id="g1", client=client)
    assert client.containers.run_calls == []  # never spawned


def test_spawn_volume_without_source_refuses():
    bad = {"Type": "volume", "Destination": "/data"}  # no Name/Source
    client = _client([bad])
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id="g1", client=client)


def test_spawn_self_not_found_refuses():
    containers = _FakeContainers({"Mounts": [_VOL_DATA]})
    containers.get = MagicMock(side_effect=docker.errors.NotFound("nope"))
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id="g1",
                              client=_FakeClient(containers))


def test_spawn_run_api_error_refuses():
    client = _client([_VOL_DATA], run_exc=docker.errors.APIError("daemon boom"))
    with pytest.raises(updater.UpdaterSpawnError):
        updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id="g1", client=client)


def test_spawn_none_greffer_id_becomes_empty_env():
    client = _client([_VOL_DATA])
    updater.spawn_updater(image=_IMG, target_tag="0.3.6", greffer_id=None, client=client)
    assert client.containers.run_calls[0]["environment"]["GREFFER_ID"] == ""


# --- update_in_progress lock probe (HLD section 10) -----------------

def test_update_in_progress_false_when_unlocked(tmp_path):
    assert updater.update_in_progress(tmp_path / ".update.lock") is False


def test_update_in_progress_true_when_held(tmp_path):
    import fcntl
    lock = tmp_path / ".update.lock"
    fh = open(lock, "w", encoding="utf-8")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # another actor holds it
    try:
        assert updater.update_in_progress(lock) is True
    finally:
        fh.close()


def test_update_in_progress_false_when_unopenable(tmp_path):
    # a path under a missing dir can't be opened -> can't tell -> do not block
    assert updater.update_in_progress(tmp_path / "missing" / ".update.lock") is False
