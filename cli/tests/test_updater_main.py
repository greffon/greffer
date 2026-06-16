"""Tests for the updater container entrypoint (greffer_cli.updater.__main__).

The engine and the /data lock are injected, so no real docker, cosign, or
fcntl. Focus: env -> config mapping, the usage/missing-env/lock-held refusal
paths, and that the lock is always released (try/finally).
"""

from __future__ import annotations

from pathlib import Path

from greffer_cli.updater import __main__ as entry
from greffer_cli.updater import engine

_ENV = {
    "GREFFER_VERSION_MANIFEST_URL": "https://x/m.json",
    "GREFFER_ID": "g1",
    "GREFFER_MODE": "tunnel",
}


def test_config_from_env_maps_fields():
    cfg = entry._config_from_env("0.3.6", dict(_ENV))
    assert cfg["target_tag"] == "0.3.6"
    assert cfg["manifest_url"] == "https://x/m.json"
    assert cfg["greffer_id"] == "g1"
    assert cfg["mode"] == "tunnel"
    assert cfg["compose_file"] == entry.DEFAULT_COMPOSE
    assert cfg["ratchet_path"] == entry.DEFAULT_RATCHET
    assert cfg["cosign_pub"] == entry.DEFAULT_COSIGN_PUB
    assert cfg["timeout"] == 600.0


def test_config_defaults_mode_proxy_when_blank():
    env = dict(_ENV)
    env["GREFFER_MODE"] = ""
    assert entry._config_from_env("0.3.6", env)["mode"] == "proxy"


def test_baked_baseline_env_overrides_file():
    env = {"GREFFER_MIN_SUPPORTED_BASELINE": "  0.3.2  "}
    assert entry._baked_baseline(env) == "0.3.2"


def test_baked_baseline_none_when_absent(monkeypatch):
    # No env override and the baked file is absent -> None.
    monkeypatch.setattr(entry, "DEFAULT_BASELINE_FILE", Path("/nonexistent/baseline"))
    assert entry._baked_baseline({}) is None


def test_main_happy_runs_engine_and_releases_lock():
    calls = {}
    handle = object()

    def fake_run(**cfg):
        calls["cfg"] = cfg
        return engine.EXIT_OK

    released = []
    rc = entry.main(
        ["0.3.6"], env=dict(_ENV), run=fake_run,
        lock_acquire=lambda: handle, lock_release=released.append,
    )
    assert rc == engine.EXIT_OK
    assert calls["cfg"]["target_tag"] == "0.3.6"
    assert released == [handle]  # lock released in finally


def test_main_usage_error_refuses():
    rc = entry.main([], env=dict(_ENV), run=lambda **k: pytest_fail())
    assert rc == engine.EXIT_REFUSED


def test_main_empty_tag_refuses():
    rc = entry.main([""], env=dict(_ENV), run=lambda **k: pytest_fail())
    assert rc == engine.EXIT_REFUSED


def test_main_missing_env_refuses_before_lock():
    locked = {"acquired": False}
    rc = entry.main(
        ["0.3.6"], env={}, run=lambda **k: pytest_fail(),
        lock_acquire=lambda: locked.__setitem__("acquired", True),
    )
    assert rc == engine.EXIT_REFUSED
    assert locked["acquired"] is False  # never reached the lock


def test_main_lock_held_refuses_without_running():
    rc = entry.main(
        ["0.3.6"], env=dict(_ENV), run=lambda **k: pytest_fail(),
        lock_acquire=lambda: None,  # another actor holds it
    )
    assert rc == engine.EXIT_REFUSED


def test_main_releases_lock_even_when_engine_raises():
    released = []
    handle = object()

    def boom(**cfg):
        raise RuntimeError("engine blew up")

    try:
        entry.main(["0.3.6"], env=dict(_ENV), run=boom,
                   lock_acquire=lambda: handle, lock_release=released.append)
    except RuntimeError:
        pass
    assert released == [handle]  # released despite the exception


def pytest_fail():
    raise AssertionError("run should not have been called")
