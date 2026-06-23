"""Tests for the heartbeat worker (greffer-observability epic, Feature #1)."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import mock_open, patch

import pytest

# Force import of the lazy compose submodule so its attribute exists on its
# parent package before patching (mirrors test_workers_monitor).
import apps.utils.docker.compose  # noqa: F401

from app.main import create_app
from app.settings import Settings
from app.workers.heartbeat import (
    _collect_or_reuse,
    _host_cpu_pct,
    _one_heartbeat,
    _read_cpu_sample,
    _read_meminfo,
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
    assert body["cert_serial"] is None  # no cert installed yet -> None
    # No update lock held under a fresh tmp greffon_path -> reports not-updating.
    assert body["update_in_progress"] is False


def test_one_heartbeat_reports_installed_cert_serial(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="hb-tok", settings=settings)
    app.state.status_map = {"map": {}, "at": time.monotonic()}
    app.state.installed_cert_serial = "a1b2c3"  # set by a prior cert install

    with patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)

    body = mock_requests.post.call_args.kwargs["json"]
    assert body["cert_serial"] == "a1b2c3"  # reported for DR reconciliation (R-DR10)


def test_one_heartbeat_reports_update_in_progress(
    settings: Settings, tmp_path
) -> None:
    """When a self-update holds the /data lock, the beat reports it so the manager
    can keep ``updating`` true (and later clear it when this flips back to False)."""
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="hb-tok", settings=settings)
    app.state.status_map = {"map": {}, "at": time.monotonic()}

    with patch("app.workers.heartbeat.requests") as mock_requests, \
            patch("apps.utils.docker.updater.update_in_progress",
                  return_value=True) as probe:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)

    body = mock_requests.post.call_args.kwargs["json"]
    assert body["update_in_progress"] is True
    # Probes the SAME path the controller 409s on (HLD section 10).
    assert probe.call_args.args[0] == tmp_path / ".update.lock"


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


# --- Host vitals (resource-monitoring epic, Feature 1) -------------------

_MEMINFO = (
    "MemTotal:       16384 kB\n"
    "MemFree:         1000 kB\n"
    "MemAvailable:    4096 kB\n"
    "Buffers:          200 kB\n"
)


def test_read_meminfo_parses_used_and_total() -> None:
    with patch("builtins.open", mock_open(read_data=_MEMINFO)):
        used, total = _read_meminfo()
    assert total == 16384 * 1024
    # used = MemTotal - MemAvailable
    assert used == (16384 - 4096) * 1024


def test_read_meminfo_none_when_memavailable_absent() -> None:
    data = "MemTotal:       16384 kB\nMemFree:         1000 kB\n"
    with patch("builtins.open", mock_open(read_data=data)):
        assert _read_meminfo() == (None, None)


def test_read_meminfo_none_on_oserror() -> None:
    with patch("builtins.open", side_effect=OSError("no /proc")):
        assert _read_meminfo() == (None, None)


def test_read_meminfo_clamps_negative_used_to_zero() -> None:
    # MemAvailable can momentarily exceed MemTotal accounting; never report
    # a negative used.
    data = "MemTotal:       1000 kB\nMemAvailable:    2000 kB\n"
    with patch("builtins.open", mock_open(read_data=data)):
        used, total = _read_meminfo()
    assert used == 0
    assert total == 1000 * 1024


def test_read_cpu_sample_parses_total_and_idle() -> None:
    data = "cpu  100 200 300 400 50 0 0 0 0 0\ncpu0 1 2 3 4 5\n"
    with patch("builtins.open", mock_open(read_data=data)):
        sample = _read_cpu_sample()
    assert sample == (1050, 450)  # total = sum, idle = idle(400) + iowait(50)


def test_read_cpu_sample_none_on_bad_line() -> None:
    with patch("builtins.open", mock_open(read_data="garbage line\n")):
        assert _read_cpu_sample() is None


def test_read_cpu_sample_none_on_oserror() -> None:
    with patch("builtins.open", side_effect=OSError("no /proc")):
        assert _read_cpu_sample() is None


def test_read_cpu_sample_none_on_short_line() -> None:
    # Fewer than idle+iowait columns (old 2.4 kernels): reject rather than
    # index past the end. Locks the len(parts) < 6 guard.
    with patch("builtins.open", mock_open(read_data="cpu 1 2 3\n")):
        assert _read_cpu_sample() is None


def test_host_cpu_pct_first_beat_seeds_then_computes_delta(
    settings: Settings,
) -> None:
    app = create_app(token="t", settings=settings)
    with patch(
        "app.workers.heartbeat._read_cpu_sample",
        side_effect=[(1000, 800), (2000, 1000)],
    ):
        first = _host_cpu_pct(app)
        second = _host_cpu_pct(app)
    assert first is None  # no prior sample: seeds baseline, reports nothing
    # delta_total=1000, delta_idle=200 -> busy = 80.0%
    assert second == 80.0


def test_host_cpu_pct_none_on_no_elapsed_jiffies(settings: Settings) -> None:
    app = create_app(token="t", settings=settings)
    with patch(
        "app.workers.heartbeat._read_cpu_sample",
        side_effect=[(1000, 800), (1000, 800)],
    ):
        _host_cpu_pct(app)
        second = _host_cpu_pct(app)
    assert second is None  # delta_total == 0


def test_host_cpu_pct_clamps_on_counter_regression(settings: Settings) -> None:
    # If idle grows more than total between samples (a counter reset/reboot
    # mid-run), busy would go negative; the clamp must floor it at 0.0, never
    # emit a negative the manager would reject.
    app = create_app(token="t", settings=settings)
    with patch(
        "app.workers.heartbeat._read_cpu_sample",
        side_effect=[(2000, 1000), (2100, 1500)],  # total +100, idle +500
    ):
        _host_cpu_pct(app)
        second = _host_cpu_pct(app)
    assert second == 0.0


def test_host_cpu_pct_none_when_proc_unreadable(settings: Settings) -> None:
    app = create_app(token="t", settings=settings)
    with patch("app.workers.heartbeat._read_cpu_sample", return_value=None):
        assert _host_cpu_pct(app) is None


def test_one_heartbeat_includes_host_vitals(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = {"map": {}, "at": time.monotonic()}
    with patch("app.workers.heartbeat._read_meminfo",
               return_value=(2_000_000, 8_000_000)), \
            patch("app.workers.heartbeat._host_cpu_pct", return_value=42.0), \
            patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    body = mock_requests.post.call_args.kwargs["json"]
    assert body["cpu_pct"] == 42.0
    assert body["mem_used_bytes"] == 2_000_000
    assert body["mem_total_bytes"] == 8_000_000


def test_one_heartbeat_degraded_still_sends_host_vitals(
    settings: Settings, tmp_path
) -> None:
    # Host vitals come from /proc, independent of the docker collection that
    # sets degraded, so a degraded beat still carries them.
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = None
    with patch("app.workers.heartbeat.collect_status_map",
               side_effect=RuntimeError("boom")), \
            patch("app.workers.heartbeat._read_meminfo",
                  return_value=(3_000_000, 8_000_000)), \
            patch("app.workers.heartbeat._host_cpu_pct", return_value=55.0), \
            patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    body = mock_requests.post.call_args.kwargs["json"]
    assert body["degraded"] is True
    assert body["cpu_pct"] == 55.0
    assert body["mem_used_bytes"] == 3_000_000
    assert body["mem_total_bytes"] == 8_000_000


def test_one_heartbeat_host_vitals_null_when_unreadable(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = {"map": {}, "at": time.monotonic()}
    with patch("app.workers.heartbeat._read_meminfo",
               return_value=(None, None)), \
            patch("app.workers.heartbeat._read_cpu_sample",
                  return_value=None), \
            patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    body = mock_requests.post.call_args.kwargs["json"]
    assert body["cpu_pct"] is None
    assert body["mem_used_bytes"] is None
    assert body["mem_total_bytes"] is None


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


@pytest.mark.asyncio
async def test_heartbeat_worker_seq_increments_across_beats(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="t", settings=settings)
    app.state.registered.set()
    seqs = []

    def _fake(_app, seq):
        seqs.append(seq)
        return 200

    async def _sleep(_s):
        if len(seqs) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)
    monkeypatch.setattr("app.workers.heartbeat.asyncio.sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_worker(app)

    assert seqs == [1, 2, 3]


@pytest.mark.asyncio
async def test_heartbeat_worker_swallows_network_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests as _requests
    app = create_app(token="t", settings=settings)
    app.state.registered.set()
    calls = 0

    def _fake(_app, _seq):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _requests.ConnectionError("manager down")
        return 200

    async def _sleep(_s):
        if calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)
    monkeypatch.setattr("app.workers.heartbeat.asyncio.sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_worker(app)

    # A network error does not pause beating or trigger re-register.
    assert calls == 2
    assert not app.state.reregister_requested.is_set()
    assert app.state.registered.is_set()


def test_one_heartbeat_degraded_payload_has_captured_at(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = None
    with patch("app.workers.heartbeat.collect_status_map",
               side_effect=RuntimeError("boom")), \
            patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    body = mock_requests.post.call_args.kwargs["json"]
    assert body["captured_at"]  # always present, even on a degraded beat


@pytest.mark.asyncio
async def test_seq_and_boot_id_stable_across_403_resume(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seq keeps incrementing (not reset) and boot_id stays stable across a
    403 pause/resume — the manager's restart-safe ordering depends on it."""
    app = create_app(token="t", settings=settings)
    app.state.registered.set()
    seqs = []
    boot_ids = []

    def _fake(_app, seq):
        seqs.append(seq)
        boot_ids.append(_app.state.boot_id)
        return 403 if len(seqs) == 1 else 200

    async def _sleep(_s):
        if len(seqs) == 1:
            app.state.registered.set()  # re-arm to resume after the 403 pause
        elif len(seqs) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.workers.heartbeat._one_heartbeat", _fake)
    monkeypatch.setattr("app.workers.heartbeat.asyncio.sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_worker(app)

    assert seqs == [1, 2]  # seq not reset on resume
    assert boot_ids == [app.state.boot_id, app.state.boot_id]  # stable


def test_heartbeat_reuses_monitor_map_no_double_sweep(
    settings: Settings, tmp_path
) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    app = create_app(token="t", settings=settings)
    app.state.status_map = {
        "map": {"inst-a": "running"}, "at": time.monotonic(),
        "captured_at": "2026-06-12T00:00:00+00:00"}
    with patch("app.workers.heartbeat.collect_status_map") as m, \
            patch("app.workers.heartbeat.requests") as mock_requests:
        mock_requests.post.return_value.status_code = 200
        _one_heartbeat(app, 1)
    m.assert_not_called()  # reused the monitor sweep, no second docker sweep
    assert mock_requests.post.call_args.kwargs["json"]["instances"] == {
        "inst-a": "running"}
