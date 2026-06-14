"""Unit tests for the readiness evaluation (greffer-observability Feature #3)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.readiness import evaluate_readiness


def _app(registered: bool = True, worker_tasks: dict | None = None):
    ev = asyncio.Event()
    if registered:
        ev.set()
    return SimpleNamespace(
        state=SimpleNamespace(registered=ev, worker_tasks=worker_tasks or {}))


def _task(done: bool):
    t = Mock()
    t.done.return_value = done
    return t


def test_ready_when_all_good():
    with patch("app.readiness._docker_ok", return_value=True):
        r = evaluate_readiness(_app(registered=True, worker_tasks={}))
    assert not r.fatal
    assert r.status == "ready"
    assert r.reasons == []


def test_degraded_when_registration_pending():
    # A greffer awaiting acceptance is healthy, NOT fatal — it must not be
    # restart-looped by the watchdog.
    with patch("app.readiness._docker_ok", return_value=True):
        r = evaluate_readiness(_app(registered=False))
    assert not r.fatal
    assert r.status == "degraded"
    assert r.reasons == ["registration_pending"]


def test_fatal_when_docker_unreachable():
    with patch("app.readiness._docker_ok", return_value=False):
        r = evaluate_readiness(_app(registered=True))
    assert r.fatal
    assert r.status == "fatal"
    assert "docker_unreachable" in r.reasons


def test_fatal_when_long_lived_worker_dead():
    wt = {"greffer-monitor": _task(done=True)}
    with patch("app.readiness._docker_ok", return_value=True):
        r = evaluate_readiness(_app(registered=True, worker_tasks=wt))
    assert r.fatal
    assert "worker_dead:greffer-monitor" in r.reasons


def test_fatal_when_reregister_supervisor_dead():
    # A dead reregister supervisor permanently wedges 403-recovery (heartbeats
    # park forever with no consumer of reregister_requested), so it is fatal
    # (codex P2 on #72).
    wt = {"greffer-reregister": _task(done=True)}
    with patch("app.readiness._docker_ok", return_value=True):
        r = evaluate_readiness(_app(registered=True, worker_tasks=wt))
    assert r.fatal
    assert "worker_dead:greffer-reregister" in r.reasons


def test_alive_long_lived_worker_not_fatal():
    wt = {"greffer-heartbeat": _task(done=False)}
    with patch("app.readiness._docker_ok", return_value=True):
        r = evaluate_readiness(_app(registered=True, worker_tasks=wt))
    assert not r.fatal


def test_one_shot_register_done_is_not_fatal():
    # ``register`` exits normally after success; its done() must NOT be fatal
    # (only the forever-loop workers count).
    wt = {"greffer-register": _task(done=True),
          "greffer-monitor": _task(done=False)}
    with patch("app.readiness._docker_ok", return_value=True):
        r = evaluate_readiness(_app(registered=True, worker_tasks=wt))
    assert not r.fatal
    assert not any("worker_dead" in reason for reason in r.reasons)
