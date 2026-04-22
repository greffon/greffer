"""Tests for the async monitor_worker and its sync tick.

Key behavior locked in:
- change_status only fires on a status *change* (not every tick).
- An exception in one tick does NOT kill the worker (deviation from
  legacy which had the try/except outside the while loop).
- Cancellation during ``asyncio.sleep()`` exits cleanly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# Force import of the lazy submodules so their attributes exist on their
# parent packages. Without this, `patch("apps.utils.docker.compose")` raises
# AttributeError — Python only attaches submodules to their parent package
# namespace AFTER first import.
import apps.utils.docker.compose  # noqa: F401
import apps.utils.greffon.base_server  # noqa: F401

from app.main import create_app
from app.settings import Settings
from app.workers.monitor import _one_monitor_tick, monitor_worker


# ---------------------------------------------------------------------------
# Sync tick
# ---------------------------------------------------------------------------


def test_one_tick_calls_change_status_on_first_seen(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "inst-a").mkdir()
    settings.greffon_path = tmp_path  # type: ignore[misc]
    prev: dict[str, str] = {}

    with patch("apps.utils.docker.compose") as mock_compose, patch(
        "apps.utils.greffon.base_server"
    ) as mock_base:
        mock_compose.get_status.return_value = {"status": "running"}
        _one_monitor_tick(settings, prev)

    mock_base.change_status.assert_called_once_with("inst-a", "running")
    assert prev == {"inst-a": "running"}


def test_one_tick_skips_change_status_when_unchanged(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "inst-a").mkdir()
    settings.greffon_path = tmp_path  # type: ignore[misc]
    prev = {"inst-a": "running"}

    with patch("apps.utils.docker.compose") as mock_compose, patch(
        "apps.utils.greffon.base_server"
    ) as mock_base:
        mock_compose.get_status.return_value = {"status": "running"}
        _one_monitor_tick(settings, prev)

    mock_base.change_status.assert_not_called()


def test_one_tick_fires_change_status_on_transition(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "inst-a").mkdir()
    settings.greffon_path = tmp_path  # type: ignore[misc]
    prev = {"inst-a": "running"}

    with patch("apps.utils.docker.compose") as mock_compose, patch(
        "apps.utils.greffon.base_server"
    ) as mock_base:
        mock_compose.get_status.return_value = {"status": "stopped"}
        _one_monitor_tick(settings, prev)

    mock_base.change_status.assert_called_once_with("inst-a", "stopped")
    assert prev == {"inst-a": "stopped"}


# ---------------------------------------------------------------------------
# Async worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_worker_continues_after_tick_exception(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEVIATION FROM LEGACY: a failing tick does not kill the worker.

    This test would have FAILED against the legacy sync version which
    places try/except outside the while loop. Intentional fix per HLD #3.
    """
    app = create_app(token="t", settings=settings)

    call_count = 0

    def _fake_tick(_settings, _prev):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        # Second call raises CancelledError via asyncio.sleep below

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
    # Let the task reach the sleep.
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_monitor_worker_cancel_is_snappy_during_blocking_tick(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: If ``anyio.to_thread.run_sync`` were called without
    ``abandon_on_cancel=True``, cancellation during a hung ``requests.get``
    would block lifespan shutdown until the call eventually returned.

    This test simulates a blocking tick and verifies cancellation returns
    within a short budget. Without the fix, the test would hang for
    several seconds waiting for the thread.
    """
    import threading
    import time

    app = create_app(token="t", settings=settings)
    tick_started = threading.Event()
    never_complete = threading.Event()

    def _blocking_tick(_settings, _prev):
        tick_started.set()
        # Block the thread until test-teardown releases it.
        never_complete.wait(timeout=10)

    monkeypatch.setattr(
        "app.workers.monitor._one_monitor_tick", _blocking_tick
    )

    task = asyncio.create_task(monitor_worker(app))
    # Wait until the blocking tick has actually started in the threadpool.
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
    # Must return in well under the 10s fake-block. Small budget chosen
    # to catch regressions — on a healthy machine abandon_on_cancel
    # returns in a few ms.
    assert cancel_duration < 1.0, f"cancel took {cancel_duration}s"

    # Cleanup: unblock the leaked thread so pytest doesn't hang on exit.
    never_complete.set()
