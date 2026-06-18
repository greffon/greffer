"""Tests for the v2 :latest updater entrypoint (updater.__main__).

No positional args (the model is "update to latest"); config comes from the env;
the /data lock gates concurrency. The engine + lock are injected, so no real
docker, cosign, or fcntl.
"""

from __future__ import annotations

import pytest

from greffer_cli.updater import __main__ as entry
from greffer_cli.updater import engine


def test_config_from_env_defaults():
    cfg = entry._config_from_env({})
    assert cfg["cosign_pub"] == entry.DEFAULT_COSIGN_PUB
    assert cfg["greffer_id"] is None
    assert cfg["target_tag"] is None  # absent -> the engine defaults to latest
    assert cfg["timeout"] == 600.0


def test_config_from_env_overrides():
    cfg = entry._config_from_env(
        {"GREFFER_COSIGN_PUB": "/x", "GREFFER_ID": "g9",
         "GREFFER_UPDATER_TARGET_TAG": "0.3.6", "GREFFER_UPDATER_TIMEOUT": "120"})
    assert cfg == {"cosign_pub": "/x", "greffer_id": "g9",
                   "target_tag": "0.3.6", "timeout": 120.0}


def test_config_blank_target_tag_is_none():
    # an empty GREFFER_UPDATER_TARGET_TAG must normalize to None (-> latest),
    # not the empty string (which would build an invalid `<repo>:` ref)
    assert entry._config_from_env({"GREFFER_UPDATER_TARGET_TAG": ""})["target_tag"] is None


def test_main_takes_lock_runs_engine_releases():
    seen, released = {}, {"v": False}

    def fake_run(**cfg):
        seen.update(cfg)
        return engine.EXIT_OK
    rc = entry.main(
        env={"GREFFER_ID": "g1"}, run=fake_run,
        lock_acquire=lambda: object(),
        lock_release=lambda h: released.__setitem__("v", True))
    assert rc == engine.EXIT_OK
    assert seen["greffer_id"] == "g1" and seen["cosign_pub"] == entry.DEFAULT_COSIGN_PUB
    assert released["v"] is True


def test_main_refuses_when_lock_held():
    def fake_run(**cfg):
        pytest.fail("engine ran while another update holds the lock")
    rc = entry.main(env={}, run=fake_run,
                    lock_acquire=lambda: None, lock_release=lambda h: None)
    assert rc == engine.EXIT_REFUSED


def test_main_proceeds_without_fcntl_sentinel():
    ran = {"v": False}

    def fake_run(**cfg):
        ran["v"] = True
        return engine.EXIT_OK
    rc = entry.main(env={}, run=fake_run,
                    lock_acquire=lambda: entry._NO_LOCK, lock_release=lambda h: None)
    assert rc == engine.EXIT_OK and ran["v"] is True


def test_release_lock_tolerates_sentinels():
    entry.release_lock(None)
    entry.release_lock(entry._NO_LOCK)  # must not raise


def test_acquire_lock_retries_through_transient_probe_hold(tmp_path):
    # A heartbeat/controller probe holds LOCK_EX for only microseconds; a one-shot
    # acquire could land in that window and wrongly refuse. acquire_lock retries,
    # so a hold released during the backoff is absorbed and the update proceeds.
    fcntl = pytest.importorskip("fcntl")
    lock = tmp_path / ".update.lock"
    holder = open(lock, "w", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Release the "probe" hold during the first backoff sleep (no real sleeping).
    handle = entry.acquire_lock(
        lock, attempts=5,
        _sleep=lambda _s: fcntl.flock(holder.fileno(), fcntl.LOCK_UN))
    assert handle is not None
    entry.release_lock(handle)
    holder.close()


def test_acquire_lock_gives_up_on_persistent_hold(tmp_path):
    # A genuine concurrent update holds the lock for its whole run, so acquire_lock
    # still refuses after the bounded retry (it does not block forever).
    fcntl = pytest.importorskip("fcntl")
    lock = tmp_path / ".update.lock"
    holder = open(lock, "w", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    sleeps = {"n": 0}
    handle = entry.acquire_lock(
        lock, attempts=3, _sleep=lambda _s: sleeps.__setitem__("n", sleeps["n"] + 1))
    assert handle is None
    assert sleeps["n"] == 2  # slept between attempts, not after the last
    holder.close()


def test_default_lock_is_data_update_lock():
    # the in-container updater must flock the SAME filename on /data that the host
    # v1 `greffer update` resolves on the volume mountpoint, else they never
    # contend (HLD §10). A change to either side's filename is caught by a test.
    # as_posix() so the assertion is OS-independent (this test also runs on the
    # Windows CI build, where str(Path) uses backslashes).
    assert entry.DEFAULT_LOCK.as_posix() == "/data/.update.lock"
    assert entry.DEFAULT_LOCK.name == ".update.lock"
