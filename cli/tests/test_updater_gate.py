"""Tests for the socket-only /readyz gate (updater.gate.health_gate).

recreate.* probes are monkeypatched (no real docker exec). A simple incrementing
clock drives the timeout; sleep is a no-op.
"""

from __future__ import annotations

import json

from greffer_cli import compose, update
from greffer_cli.updater import gate, recreate


def _readyz(status: str = "ready", rid: str = "g1", reasons=None) -> compose.CommandResult:
    return compose.CommandResult(
        0, json.dumps({"status": status, "id": rid, "reasons": reasons or []}), "")


def _conn_err() -> compose.CommandResult:
    return compose.CommandResult(1, "", "connection refused")


def _clock():
    state = {"v": 0.0}

    def now():
        cur = state["v"]
        state["v"] += 1.0
        return cur
    return now


def _wire(monkeypatch, *, readyz, running=True, image_id="applied-id", restarts=0):
    if isinstance(readyz, list):
        it = iter(readyz)
        monkeypatch.setattr(recreate, "exec_readyz", lambda name: next(it, _conn_err()))
    else:
        monkeypatch.setattr(recreate, "exec_readyz", lambda name: readyz)
    monkeypatch.setattr(recreate, "container_image_id_by_name", lambda name: image_id)
    monkeypatch.setattr(recreate, "container_running", lambda name: running)
    if isinstance(restarts, list):
        it2 = iter(restarts)
        monkeypatch.setattr(recreate, "restart_count", lambda name: next(it2, restarts[-1]))
    else:
        monkeypatch.setattr(recreate, "restart_count", lambda name: restarts)


def _gate(*, check_version=True, applied="applied-id", timeout=100.0):
    return gate.health_gate(
        "greffer-greffer-1", greffer_id="g1", applied_image_id=applied,
        service_names=["greffer-greffer-1", "greffer-nginx-1"], timeout=timeout,
        check_version=check_version, sleep=lambda _s: None, now=_clock())


def test_gate_ready(monkeypatch):
    _wire(monkeypatch, readyz=_readyz("ready"))
    assert _gate() == update.GATE_READY


def test_gate_wrong_id(monkeypatch):
    _wire(monkeypatch, readyz=_readyz("ready", rid="someone-else"))
    assert _gate() == update.GATE_WRONG_ID


def test_gate_fatal(monkeypatch):
    _wire(monkeypatch, readyz=_readyz("fatal"))
    assert _gate() == update.GATE_FATAL


def test_gate_degraded_other_fails(monkeypatch):
    _wire(monkeypatch, readyz=_readyz("degraded", reasons=["docker_unreachable"]))
    assert _gate() == update.GATE_DEGRADED_OTHER


def test_gate_waits_through_registration_pending(monkeypatch):
    _wire(monkeypatch, readyz=[
        _readyz("degraded", reasons=["registration_pending"]), _readyz("ready")])
    assert _gate() == update.GATE_READY


def test_gate_not_applied_when_running_image_differs(monkeypatch):
    _wire(monkeypatch, readyz=_readyz("ready"), image_id="some-other-image")
    assert _gate() == update.GATE_NOT_APPLIED


def test_gate_check_version_false_skips_applied(monkeypatch):
    # rollback re-gate: ready + id + running passes even though the image differs
    _wire(monkeypatch, readyz=_readyz("ready"), image_id="some-other-image")
    assert _gate(check_version=False) == update.GATE_READY


def test_gate_sibling_not_running_times_out(monkeypatch):
    _wire(monkeypatch, readyz=_readyz("ready"), running=False)
    assert _gate(timeout=5.0) == update.GATE_TIMEOUT


def test_gate_crash_loop(monkeypatch):
    # restart count climbs > 1 past the post-recreate baseline
    _wire(monkeypatch, readyz=_conn_err(), restarts=[0, 5])
    assert _gate(timeout=50.0) == update.GATE_CRASH_LOOP


def test_gate_timeout_when_never_responds(monkeypatch):
    _wire(monkeypatch, readyz=_conn_err())
    assert _gate(timeout=5.0) == update.GATE_TIMEOUT
