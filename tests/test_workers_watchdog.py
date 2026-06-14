"""Tests for the self-heal watchdog (greffer-observability Feature #3).

The watchdog is on by default and deliberately exits the process, so the
behaviour that matters: it acts ONLY on a sustained FATAL condition, NEVER on
degraded, and resets when a fatal blip clears before grace expires. ``sleep``
and ``_terminate`` are patched so no real time passes and no real SIGTERM fires.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.readiness import Readiness
from app.workers.watchdog import watchdog_worker

WD = "app.workers.watchdog"


def _app(interval: int = 0, grace: int = 0):
    return SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(
        greffer_watchdog_interval=interval, greffer_watchdog_grace=grace)))


@pytest.mark.asyncio
async def test_terminates_on_sustained_fatal():
    # grace=0: first fatal arms the timer, the second (>= grace) fires.
    fatal = Readiness(fatal=True, reasons=["docker_unreachable"])
    with patch(f"{WD}.asyncio.sleep", new=AsyncMock()), \
            patch(f"{WD}.evaluate_readiness", return_value=fatal), \
            patch(f"{WD}._terminate") as term:
        await watchdog_worker(_app(grace=0))
    term.assert_called_once()


@pytest.mark.asyncio
async def test_does_not_terminate_on_degraded():
    degraded = Readiness(fatal=False, reasons=["registration_pending"])
    # Break out after a couple of ticks via a cancelled sleep (simulates
    # shutdown) so the forever-loop test terminates.
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    with patch(f"{WD}.asyncio.sleep", new=sleep), \
            patch(f"{WD}.evaluate_readiness", return_value=degraded), \
            patch(f"{WD}._terminate") as term:
        with pytest.raises(asyncio.CancelledError):
            await watchdog_worker(_app())
    term.assert_not_called()


@pytest.mark.asyncio
async def test_resets_when_fatal_clears_before_grace():
    # Large grace so two quick fatals never reach it; then a ready reading
    # clears the timer, so no restart even though fatals were seen.
    fatal = Readiness(fatal=True, reasons=["docker_unreachable"])
    ready = Readiness(fatal=False, reasons=[])
    sleep = AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()])
    with patch(f"{WD}.asyncio.sleep", new=sleep), \
            patch(f"{WD}.evaluate_readiness",
                  side_effect=[fatal, fatal, ready, ready]), \
            patch(f"{WD}._terminate") as term:
        with pytest.raises(asyncio.CancelledError):
            await watchdog_worker(_app(grace=1000))
    term.assert_not_called()
