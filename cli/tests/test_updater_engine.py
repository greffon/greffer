"""Tests for the v2 :latest updater orchestration (engine.run_remote_update).

The recreate primitives + the gate are monkeypatched, so no real docker / cosign
/ network. Focus: fail-closed refusal (nothing recreated), the verify -> recreate
-> gate happy path, and rollback on recreate/gate/downgrade failure.
"""

from __future__ import annotations

import pytest

from greffer_cli import compose, update
from greffer_cli.updater import engine, gate, recreate

_STACK = [
    recreate.StackContainer("nginx", "nid", "greffon/greffer-nginx"),
    recreate.StackContainer("greffer", "gid", "greffon/greffer"),
    recreate.StackContainer("tunnel-sidecar", "sid", "greffon/tunnel-sidecar"),
]
_NAMES = {"nid": "/greffer-nginx-1", "gid": "/greffer-greffer-1",
          "sid": "/greffer-tunnel-sidecar-1"}
_DIGEST = "sha256:" + "d" * 64


def _setup(monkeypatch, *, forward_gate=update.GATE_READY, rollback_ok=True,
           old_version="0.3.4", new_version="0.3.5", recreate_fail_on=None,
           recreate_raises_on=None, verify_raises=False, inspect_none=False, stack=None):
    """Wire a happy stack; knobs flip individual failures. Returns a `calls`
    dict recording which primitives ran (for assertions)."""
    calls: dict = {"recreate": [], "rollback": [], "retag": [], "prune": 0,
                   "tag_previous": [], "tags": [], "restore_latest": []}
    monkeypatch.setattr(recreate, "discover_stack",
                        lambda: list(_STACK if stack is None else stack))

    def verify(repo, **k):
        calls["tags"].append(k.get("tag"))
        if verify_raises:
            raise recreate.VerifyError(f"boom {repo}")
        return _DIGEST
    monkeypatch.setattr(recreate, "verify_and_pull", verify)
    monkeypatch.setattr(recreate, "inspect_container",
                        lambda cid: None if inspect_none else {"Name": _NAMES[cid], "Image": f"sha256:old-{cid}"})
    monkeypatch.setattr(recreate, "current_image_id", lambda repo: "sha256:cur")
    monkeypatch.setattr(recreate, "tag_previous",
                        lambda repo, iid: (calls["tag_previous"].append(repo), True)[1])
    monkeypatch.setattr(recreate, "retag_latest",
                        lambda repo, d: (calls["retag"].append(repo), True)[1])

    def do_recreate(c, **k):
        calls["recreate"].append(c.service)
        if recreate_raises_on and c.service == recreate_raises_on:
            raise RuntimeError(f"boom recreating {c.service}")
        return not (recreate_fail_on and c.service == recreate_fail_on)
    monkeypatch.setattr(recreate, "recreate_one", do_recreate)

    def do_rollback(c, ins, oid, **k):
        calls["rollback"].append(c.service)
        return rollback_ok
    monkeypatch.setattr(recreate, "rollback_one", do_rollback)
    monkeypatch.setattr(recreate, "restore_latest",
                        lambda repo, iid: (calls["restore_latest"].append(repo), True)[1])
    monkeypatch.setattr(recreate, "dangling_prune",
                        lambda: calls.__setitem__("prune", calls["prune"] + 1))
    monkeypatch.setattr(recreate, "exec_version",
                        lambda ref: old_version if ref == "gid" else new_version)
    monkeypatch.setattr(compose, "image_id", lambda ref: "applied-id")

    def fake_gate(*a, check_version=True, **k):
        # forward gate returns the scenario outcome; rollback re-gate (no version
        # check) reports the rolled-back stack healthy.
        return forward_gate if check_version else update.GATE_READY
    monkeypatch.setattr(gate, "health_gate", fake_gate)
    return calls


def _run(target_tag=None):
    return engine.run_remote_update(
        cosign_pub="/k", greffer_id="g1", target_tag=target_tag,
        sleep=lambda _s: None, now=lambda: 0.0)


def test_happy_path_recreates_all_and_prunes(monkeypatch):
    calls = _setup(monkeypatch)
    assert _run() == engine.EXIT_OK
    assert calls["recreate"] == ["nginx", "greffer", "tunnel-sidecar"]  # §6 order
    assert calls["retag"] == ["greffon/greffer-nginx", "greffon/greffer", "greffon/tunnel-sidecar"]
    assert calls["prune"] == 1
    assert calls["rollback"] == []


def test_no_stack_refuses(monkeypatch):
    _setup(monkeypatch, stack=[])
    assert _run() == engine.EXIT_REFUSED


def test_stack_without_greffer_refuses(monkeypatch):
    calls = _setup(monkeypatch, stack=[recreate.StackContainer("nginx", "nid", "greffon/greffer-nginx")])
    assert _run() == engine.EXIT_REFUSED
    assert calls["recreate"] == []  # nothing recreated


def test_verify_failure_refuses_without_moving_latest(monkeypatch):
    calls = _setup(monkeypatch, verify_raises=True)
    assert _run() == engine.EXIT_REFUSED
    # fail-closed: no :latest moved, nothing recreated
    assert calls["retag"] == []
    assert calls["recreate"] == []


def test_inspect_failure_refuses_without_mutation(monkeypatch):
    calls = _setup(monkeypatch, inspect_none=True)
    assert _run() == engine.EXIT_REFUSED
    assert calls["retag"] == [] and calls["recreate"] == []


def test_recreate_failure_rolls_back(monkeypatch):
    calls = _setup(monkeypatch, recreate_fail_on="greffer")
    assert _run() == engine.EXIT_FAILED_ROLLED_BACK
    # nginx + greffer were attempted; rollback ran (reverse order) and prune did not
    assert "greffer" in calls["recreate"]
    assert calls["rollback"] == ["greffer", "nginx"]
    assert calls["prune"] == 0


def test_gate_failure_rolls_back(monkeypatch):
    calls = _setup(monkeypatch, forward_gate=update.GATE_FATAL)
    assert _run() == engine.EXIT_FAILED_ROLLED_BACK
    assert calls["rollback"] == ["tunnel-sidecar", "greffer", "nginx"]
    assert calls["prune"] == 0


def test_downgrade_after_ready_rolls_back(monkeypatch):
    # gate READY but the new greffer reports an OLDER version -> roll back, no prune
    calls = _setup(monkeypatch, old_version="0.3.5", new_version="0.3.4")
    assert _run() == engine.EXIT_FAILED_ROLLED_BACK
    assert calls["prune"] == 0
    assert calls["rollback"] == ["tunnel-sidecar", "greffer", "nginx"]


def test_non_numeric_version_is_not_treated_as_downgrade(monkeypatch):
    # attacker-forgeable / non-semver versions must not be compared (gate passes)
    calls = _setup(monkeypatch, old_version="0.3.5", new_version="latest")
    assert _run() == engine.EXIT_OK
    assert calls["prune"] == 1


def test_rollback_failure_returns_rollback_failed(monkeypatch):
    _setup(monkeypatch, forward_gate=update.GATE_FATAL, rollback_ok=False)
    assert _run() == engine.EXIT_FAILED_ROLLBACK_FAILED


def test_is_downgrade_helper():
    assert engine._is_downgrade("0.3.4", "0.3.5") is True
    assert engine._is_downgrade("0.3.5", "0.3.4") is False
    assert engine._is_downgrade("0.3.5", "0.3.5") is False
    assert engine._is_downgrade("0.3.10", "0.3.9") is False  # dotted-numeric, not string
    assert engine._is_downgrade("0.3", "0.3.0") is False     # length-padded -> equal
    assert engine._is_downgrade("0.2", "0.3.0") is True      # length-padded -> 0.2.0 < 0.3.0
    assert engine._is_downgrade("latest", "0.3.5") is False  # non-numeric -> not a downgrade
    assert engine._is_downgrade(None, "0.3.5") is False


# --- target_tag threading + pre-run downgrade refusal ---------------

def test_default_tag_is_latest(monkeypatch):
    calls = _setup(monkeypatch)
    assert _run() == engine.EXIT_OK
    assert calls["tags"] == ["latest", "latest", "latest"]  # one per image


def test_target_tag_is_threaded_to_verify(monkeypatch):
    calls = _setup(monkeypatch, old_version="0.3.4", new_version="0.3.6")
    assert _run(target_tag="0.3.6") == engine.EXIT_OK
    assert calls["tags"] == ["0.3.6", "0.3.6", "0.3.6"]  # same version for all -> cohesive


def test_pre_run_downgrade_refused_before_pull(monkeypatch):
    calls = _setup(monkeypatch, old_version="0.3.5")
    assert _run(target_tag="0.3.4") == engine.EXIT_REFUSED
    # refused BEFORE phase 1: nothing verified, pulled, or recreated
    assert calls["tags"] == [] and calls["recreate"] == [] and calls["rollback"] == []


def test_non_numeric_target_skips_pre_run_check(monkeypatch):
    # "latest" is not comparable, so the pre-run check is skipped and the update
    # proceeds (the post-gate guard is the backstop)
    calls = _setup(monkeypatch, old_version="0.3.5", new_version="0.3.6")
    assert _run(target_tag="latest") == engine.EXIT_OK
    assert calls["tags"] == ["latest", "latest", "latest"]


def test_equal_target_is_not_a_downgrade(monkeypatch):
    # target == current passes (idempotent re-recreate), not refused
    calls = _setup(monkeypatch, old_version="0.3.5", new_version="0.3.5")
    assert _run(target_tag="0.3.5") == engine.EXIT_OK
    assert calls["recreate"] == ["nginx", "greffer", "tunnel-sidecar"]


def test_rollback_restores_latest_to_old_image(monkeypatch):
    # on rollback, :latest is repointed at the old image (not just the container
    # recreated), so a later `docker compose up` cannot re-apply the rejected image
    calls = _setup(monkeypatch, forward_gate=update.GATE_FATAL)
    assert _run() == engine.EXIT_FAILED_ROLLED_BACK
    assert calls["restore_latest"] == [
        "greffon/tunnel-sidecar", "greffon/greffer", "greffon/greffer-nginx"]  # reverse


def test_unexpected_exception_in_mutating_window_rolls_back(monkeypatch):
    # a RAISE mid-recreate (not just a False return) must still roll back, never
    # escape run_remote_update leaving the stack half-updated
    calls = _setup(monkeypatch, recreate_raises_on="greffer")
    assert _run() == engine.EXIT_FAILED_ROLLED_BACK
    assert calls["recreate"][:2] == ["nginx", "greffer"]
    # only nginx reached `done` before greffer raised -> only nginx rolled back
    assert calls["rollback"] == ["nginx"]
    assert calls["prune"] == 0
