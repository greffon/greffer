"""Tests for the heartbeat worker (greffer-observability epic, Feature #1)."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

# Force import of the lazy compose submodule so its attribute exists on its
# parent package before patching (mirrors test_workers_monitor).
import apps.utils.docker.compose  # noqa: F401

from app.main import create_app
from app.settings import Settings
from app.workers.heartbeat import (
    _collect_or_reuse,
    _one_heartbeat,
    heartbeat_worker,
)


def test_one_heartbeat_posts_payload_with_token(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="hb-tok", settings=settings)
    app.state.status_map = {
        "map": {"inst-a": "running"}, "at": time.monotonic()}

    with patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        code = _one_heartbeat(app, 7)

    assert code == 200
    url, = mock_requests.post.call_args.args
    kwargs = mock_requests.post.call_args.kwargs
    assert url.endswith(f"/api/greffer/{settings.greffer_id}/heartbeat/")
    assert kwargs["headers"] == {"X-Greffer-Token": "hb-tok"}
    assert kwargs["verify"] == settings.greffer_ssl_verify
    assert kwargs["timeout"] == 10.0
    body = kwargs["json"]
    assert body["seq"] == 7
    assert body["boot_id"] == app.state.boot_id
    assert body["instances"] == {"inst-a": "running"}
    assert body["degraded"] is False
    assert body["interval"] == settings.heartbeat_interval
    assert "captured_at" in body and "uptime_s" in body


def test_collect_or_reuse_uses_fresh_cache(settings: Settings) -> None:
    app = create_app(token="t", settings=settings)
    app.state.status_map = {"map": {"x": "running"}, "at": time.monotonic()}
    m, degraded, reasons, _cap = _collect_or_reuse(app, settings)
    assert m == {"x": "running"}
    assert degraded is False
    assert reasons == []


def test_collect_or_reuse_collects_when_stale(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    (tmp_path / "inst-a").mkdir()
    app = create_app(token="t", settings=settings)
    app.state.status_map = {
        "map": {"old": "stopped"}, "at": time.monotonic() - 999}

    with patch("apps.utils.docker.compose") as mock_compose:
        mock_compose.get_status.return_value = {"status": "running"}
        m, degraded, reasons, _cap = _collect_or_reuse(app, settings)

    assert m == {"inst-a": "running"}
    assert degraded is False


def test_collect_or_reuse_degraded_on_failure(settings: Settings) -> None:
    app = create_app(token="t", settings=settings)
    app.state.status_map = None
    with patch(
        "app.workers.heartbeat.collect_status_map",
        side_effect=RuntimeError("boom"),
    ):
        m, degraded, reasons, _cap = _collect_or_reuse(app, settings)
    assert m == {}
    assert degraded is True
    assert "docker_unreachable" in reasons


@pytest.mark.asyncio
async def test_heartbeat_worker_requests_reregister_on_403(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="t", settings=settings)
    app.state.registered.set()  # past initial registration, so it beats

    def _fake(_app, _seq):
        return 403

    async def _sleep_then_cancel(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)
    monkeypatch.setattr(
        "app.workers.heartbeat.asyncio.sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_worker(app)

    assert app.state.reregister_requested.is_set()
    # 403 pauses beating until re-registration sets `registered` again.
    assert not app.state.registered.is_set()


@pytest.mark.asyncio
async def test_heartbeat_worker_continues_after_exception(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="t", settings=settings)
    app.state.registered.set()
    calls = 0

    def _fake(_app, _seq):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return 200

    async def _sleep(_s):
        if calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)
    monkeypatch.setattr("app.workers.heartbeat.asyncio.sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_worker(app)

    assert calls == 2  # second beat ran despite the first raising


@pytest.mark.asyncio
async def test_heartbeat_worker_waits_for_registration(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before registration completes, the heartbeat must not beat at all (no
    403 storm, no reregister trigger)."""
    app = create_app(token="t", settings=settings)
    # registered intentionally NOT set.
    beats = 0

    def _fake(_app, _seq):
        nonlocal beats
        beats += 1
        return 200

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)

    task = asyncio.create_task(heartbeat_worker(app))
    await asyncio.sleep(0.05)
    assert beats == 0
    assert not app.state.reregister_requested.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_heartbeat_worker_non_403_does_not_reregister(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="t", settings=settings)
    app.state.registered.set()

    def _fake(_app, _seq):
        return 500

    async def _sleep_then_cancel(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)
    monkeypatch.setattr(
        "app.workers.heartbeat.asyncio.sleep", _sleep_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_worker(app)

    assert not app.state.reregister_requested.is_set()
    assert app.state.registered.is_set()  # still beating


def test_collect_or_reuse_uses_cached_captured_at(settings: Settings) -> None:
    app = create_app(token="t", settings=settings)
    app.state.status_map = {
        "map": {"x": "running"}, "at": time.monotonic(),
        "captured_at": "2026-06-12T00:00:00+00:00"}
    _m, _d, _r, captured = _collect_or_reuse(app, settings)
    assert captured == "2026-06-12T00:00:00+00:00"


def test_disk_free_bytes_returns_none_on_oserror(settings: Settings) -> None:
    from app.workers.heartbeat import _disk_free_bytes
    with patch("app.workers.heartbeat.shutil.disk_usage",
               side_effect=OSError("nope")):
        assert _disk_free_bytes(settings) is None


def test_one_heartbeat_healthy_payload_fields(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = {"map": {}, "at": time.monotonic()}
    with patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    body = mock_requests.post.call_args.kwargs["json"]
    assert body["version"] == settings.greffer_version
    assert body["reasons"] == []
    assert "disk_free_bytes" in body  # int or None
    assert body["degraded"] is False


def test_one_heartbeat_degraded_payload(settings: Settings, tmp_path) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = None
    with patch("app.workers.heartbeat.collect_status_map",
               side_effect=RuntimeError("boom")), \
            patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    body = mock_requests.post.call_args.kwargs["json"]
    assert body["degraded"] is True
    assert body["reasons"] == ["docker_unreachable"]
    assert body["instances"] == {}


def test_collect_or_reuse_window_boundary_reuses(
    settings: Settings, tmp_path
) -> None:
    # monitor_interval=5 + heartbeat_interval=5 -> window=10. A 7s-old cache
    # (older than heartbeat_interval but within the window) must be REUSED, not
    # re-collected — pins the deliberate monitor_interval+heartbeat_interval
    # window.
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = {
        "map": {"x": "running"}, "at": time.monotonic() - 7,
        "captured_at": "t"}
    with patch("app.workers.heartbeat.collect_status_map") as m:
        result, _d, _r, _c = _collect_or_reuse(app, settings)
    m.assert_not_called()
    assert result == {"x": "running"}
