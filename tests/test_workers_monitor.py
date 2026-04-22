"""Tests for the async monitor_worker and its sync tick.

Key behavior locked in:
- ``_report_status_change`` fires only on a status *change* (not every tick).
- An exception in one tick does NOT kill the worker (deviation from legacy
  which had try/except outside the while loop).
- Cancellation during ``asyncio.sleep()`` exits cleanly.
- Cancellation mid-blocking-tick returns within 1s (abandon_on_cancel).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# Force import of the lazy compose submodule so its attribute exists on
# its parent package. Python only attaches submodules to parent package
# namespaces after first import; patching a lazy import otherwise raises
# AttributeError.
import apps.utils.docker.compose  # noqa: F401

from app.main import create_app
from app.settings import Settings
from app.workers.monitor import _one_monitor_tick, monitor_worker


# ---------------------------------------------------------------------------
# Sync tick
# ---------------------------------------------------------------------------


def test_one_tick_calls_report_status_change_on_first_seen(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "inst-a").mkdir()
    settings.greffon_path = tmp_path  # type: ignore[misc]
    prev: dict[str, str] = {}

    with patch("apps.utils.docker.compose") as mock_compose, patch(
        "app.workers.monitor._report_status_change"
    ) as mock_report:
        mock_compose.get_status.return_value = {"status": "running"}
        _one_monitor_tick(settings, prev)

    mock_report.assert_called_once_with(settings, "inst-a", "running")
    assert prev == {"inst-a": "running"}


def test_one_tick_skips_report_status_change_when_unchanged(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "inst-a").mkdir()
    settings.greffon_path = tmp_path  # type: ignore[misc]
    prev = {"inst-a": "running"}

    with patch("apps.utils.docker.compose") as mock_compose, patch(
        "app.workers.monitor._report_status_change"
    ) as mock_report:
        mock_compose.get_status.return_value = {"status": "running"}
        _one_monitor_tick(settings, prev)

    mock_report.assert_not_called()


def test_one_tick_fires_report_status_change_on_transition(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "inst-a").mkdir()
    settings.greffon_path = tmp_path  # type: ignore[misc]
    prev = {"inst-a": "running"}

    with patch("apps.utils.docker.compose") as mock_compose, patch(
        "app.workers.monitor._report_status_change"
    ) as mock_report:
        mock_compose.get_status.return_value = {"status": "stopped"}
        _one_monitor_tick(settings, prev)

    mock_report.assert_called_once_with(settings, "inst-a", "stopped")
    assert prev == {"inst-a": "stopped"}


# ---------------------------------------------------------------------------
# _report_status_change — inlined from the deleted base_server.change_status
# ---------------------------------------------------------------------------


def test_report_status_change_posts_correct_payload(settings: Settings) -> None:
    from app.workers.monitor import _report_status_change

    with patch("app.workers.monitor.requests") as mock_requests:
        _report_status_change(settings, "inst-42", "running")

    mock_requests.post.assert_called_once()
    url, = mock_requests.post.call_args.args
    kwargs = mock_requests.post.call_args.kwargs
    assert url.endswith("/api/greffer/instances/inst-42/")
    assert kwargs["json"] == {"status": "running"}
    assert kwargs["verify"] == settings.greffer_ssl_verify
    assert kwargs["timeout"] == 10.0


# ---------------------------------------------------------------------------
# Async worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_worker_continues_after_tick_exception(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEVIATION FROM LEGACY: a failing tick does not kill the worker."""
    app = create_app(token="t", settings=settings)

    call_count = 0

    def _fake_tick(_settings, _prev):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")

    async def _sleep_once_then_cancel(_s: float) -> None:
        if call_count >= 2:
            raise asyncio.CancelledError
        return

    monkeypatch.setattr("app.workers.monitor.asyncio.sleep", _sleep_once_then_cancel)
    monkeypatch.setattr("app.workers.monitor._one_monitor_tick", _fake_tick)

    with pytest.raises(asyncio.CancelledError):
        await monitor_worker(app)

    assert call_count == 2  # second tick ran despite the first exception


@pytest.mark.asyncio
async def test_monitor_worker_cancellable_during_sleep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during asyncio.sleep propagates cleanly."""
    app = create_app(token="t", settings=settings)

    def _noop_tick(_settings, _prev):
        return

    monkeypatch.setattr("app.workers.monitor._one_monitor_tick", _noop_tick)

    task = asyncio.create_task(monitor_worker(app))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_monitor_worker_cancel_is_snappy_during_blocking_tick(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: with ``abandon_on_cancel=True``, cancellation during a
    hung blocking tick returns within 1s. Without it, the test would
    hang until the blocking call returns.
    """
    import threading
    import time

    app = create_app(token="t", settings=settings)
    tick_started = threading.Event()
    never_complete = threading.Event()

    def _blocking_tick(_settings, _prev):
        tick_started.set()
        never_complete.wait(timeout=10)

    monkeypatch.setattr(
        "app.workers.monitor._one_monitor_tick", _blocking_tick
    )

    task = asyncio.create_task(monitor_worker(app))
    for _ in range(50):
        if tick_started.is_set():
            break
        await asyncio.sleep(0.02)
    assert tick_started.is_set(), "blocking tick never started"

    cancel_t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    cancel_duration = time.monotonic() - cancel_t0
    assert cancel_duration < 1.0, f"cancel took {cancel_duration}s"

    never_complete.set()
