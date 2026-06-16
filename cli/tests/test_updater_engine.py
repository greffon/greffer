"""Tests for the v2 updater orchestration (engine.run_remote_update).

The verification primitives (provenance/floor) and the compose/recreate layer
are monkeypatched, so no real cosign, docker, or network. Focus: the fail-closed
refusal paths (nothing recreated) and the verify -> pin -> recreate -> rollback
flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greffer_cli import compose, update
from greffer_cli.updater import engine, floor, provenance

_COMPOSE = """\
services:
  greffer:
    image: greffon/greffer:0.3.5
  nginx:
    image: greffon/greffer-nginx:0.3.5
  tunnel-sidecar:
    image: greffon/tunnel-sidecar:0.3.5
"""

_ALL_OK = {
    "greffon/greffer": "0.3.5",
    "greffon/greffer-nginx": "0.3.5",
    "greffon/tunnel-sidecar": "0.3.5",
}


def _ok(out: str = "") -> compose.CommandResult:
    return compose.CommandResult(0, out, "")


@pytest.fixture
def cf(tmp_path: Path) -> Path:
    p = tmp_path / "docker-compose.yml"
    p.write_text(_COMPOSE, encoding="utf-8")
    return p


def _patch_verify(monkeypatch, *, floor_v="0.3.0", versions=None,
                  digest_ok=True, cosign_ok=True, pull_ok=True):
    versions = versions or _ALL_OK
    monkeypatch.setattr(floor, "effective_floor", lambda *a, **k: floor_v)
    monkeypatch.setattr(provenance, "resolve_digest",
                        lambda ref: ("sha256:" + "a" * 64) if digest_ok else None)
    monkeypatch.setattr(provenance, "cosign_verify", lambda repo, d, **k: cosign_ok)
    monkeypatch.setattr(provenance, "image_version",
                        lambda by_digest, **k: versions[by_digest.split("@")[0]])
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: _ok() if pull_ok else compose.CommandResult(1, "", ""))


def _run(cf, monkeypatch, **kw):
    return engine.run_remote_update(
        cf, target_tag="0.3.6", manifest_url="https://x/m.json",
        cosign_pub="/k", baked_baseline="0.3.0", ratchet_path=Path("/tmp/r"),
        greffer_id="g1", mode="tunnel", sleep=lambda _s: None, **kw)


# --- verify_and_pin -------------------------------------------------

def test_verify_and_pin_happy(cf, monkeypatch):
    _patch_verify(monkeypatch)
    verified = engine.verify_and_pin(
        cf, target_tag="0.3.6", manifest_url="https://x/m.json",
        cosign_pub="/k", baked_baseline="0.3.0", ratchet_path=Path("/tmp/r"))
    assert set(verified) == set(_ALL_OK)
    for repo, ref in verified.items():
        assert ref == f"{repo}@sha256:{'a' * 64}"


def test_verify_invalid_tag(cf, monkeypatch):
    _patch_verify(monkeypatch)
    with pytest.raises(engine.VerifyError):
        engine.verify_and_pin(
            cf, target_tag="bad:tag", manifest_url="https://x/m.json",
            cosign_pub="/k", baked_baseline="0.3.0", ratchet_path=Path("/tmp/r"))


# --- run_remote_update refusal paths (no recreate) ------------------

def test_floor_error_refuses_without_recreate(cf, monkeypatch):
    _patch_verify(monkeypatch)
    monkeypatch.setattr(floor, "effective_floor",
                        lambda *a, **k: (_ for _ in ()).throw(floor.FloorError("unreachable")))
    pinned = {"called": False}
    monkeypatch.setattr(compose, "set_image_refs",
                        lambda *a, **k: pinned.__setitem__("called", True))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED
    assert pinned["called"] is False  # nothing recreated


def test_cosign_failure_refuses(cf, monkeypatch):
    _patch_verify(monkeypatch, cosign_ok=False)
    monkeypatch.setattr(compose, "set_image_refs", lambda *a, **k: pytest.fail("recreated"))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED


def test_below_floor_refuses(cf, monkeypatch):
    bad = dict(_ALL_OK, **{"greffon/greffer-nginx": "0.2.9"})  # nginx below floor
    _patch_verify(monkeypatch, versions=bad)
    monkeypatch.setattr(compose, "set_image_refs", lambda *a, **k: pytest.fail("recreated"))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED


def test_cohesion_mismatch_refuses(cf, monkeypatch):
    mixed = dict(_ALL_OK, **{"greffon/greffer-nginx": "0.3.4"})  # different version
    _patch_verify(monkeypatch, versions=mixed)
    monkeypatch.setattr(compose, "set_image_refs", lambda *a, **k: pytest.fail("recreated"))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED


def test_digest_unresolvable_refuses(cf, monkeypatch):
    _patch_verify(monkeypatch, digest_ok=False)  # registry can't resolve a digest
    monkeypatch.setattr(compose, "set_image_refs", lambda *a, **k: pytest.fail("recreated"))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED


def test_pull_failure_refuses(cf, monkeypatch):
    _patch_verify(monkeypatch, pull_ok=False)  # docker pull by digest fails
    monkeypatch.setattr(compose, "set_image_refs", lambda *a, **k: pytest.fail("recreated"))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED


def test_empty_compose_refuses(cf, monkeypatch):
    cf.write_text("services: {}\n", encoding="utf-8")  # no greffon/* images
    _patch_verify(monkeypatch)
    monkeypatch.setattr(compose, "set_image_refs", lambda *a, **k: pytest.fail("recreated"))
    assert _run(cf, monkeypatch) == engine.EXIT_REFUSED


# --- run_remote_update recreate + rollback --------------------------

def test_happy_pins_digests_and_recreates(cf, monkeypatch):
    _patch_verify(monkeypatch)
    seen = {}
    monkeypatch.setattr(compose, "set_image_refs", lambda f, refs: seen.update(refs=refs))
    monkeypatch.setattr(compose, "compose_up", lambda f, **k: _ok())
    monkeypatch.setattr(update, "health_gate", lambda *a, **k: update.GATE_READY)
    assert _run(cf, monkeypatch) == engine.EXIT_OK
    # the compose was pinned to the verified digests, not the tag
    assert all(v == f"{r}@sha256:{'a' * 64}" for r, v in seen["refs"].items())
    assert set(seen["refs"]) == set(_ALL_OK)


def test_gate_failure_rolls_back(cf, monkeypatch):
    _patch_verify(monkeypatch)
    monkeypatch.setattr(compose, "set_image_refs", lambda f, refs: None)
    monkeypatch.setattr(compose, "compose_up", lambda f, **k: _ok())
    monkeypatch.setattr(update, "health_gate", lambda *a, **k: update.GATE_FATAL)
    monkeypatch.setattr(update, "_rollback", lambda *a, **k: update.EXIT_FAILED_ROLLED_BACK)
    assert _run(cf, monkeypatch) == engine.EXIT_FAILED_ROLLED_BACK


def test_compose_up_failure_rolls_back_without_gate(cf, monkeypatch):
    # A failed `compose up` rolls back immediately and never reaches the gate.
    _patch_verify(monkeypatch)
    monkeypatch.setattr(compose, "set_image_refs", lambda f, refs: None)
    monkeypatch.setattr(compose, "compose_up",
                        lambda f, **k: compose.CommandResult(1, "", "up failed"))
    monkeypatch.setattr(update, "health_gate",
                        lambda *a, **k: pytest.fail("gate reached after up failure"))
    monkeypatch.setattr(update, "_rollback", lambda *a, **k: update.EXIT_FAILED_ROLLED_BACK)
    assert _run(cf, monkeypatch) == engine.EXIT_FAILED_ROLLED_BACK


def test_gate_gets_verified_greffer_digest_not_tag(cf, monkeypatch):
    # v2 pins by digest, so the gate must verify "version applied" against the
    # verified greffon/greffer digest, never the (absent) local tag.
    _patch_verify(monkeypatch)
    monkeypatch.setattr(compose, "set_image_refs", lambda f, refs: None)
    monkeypatch.setattr(compose, "compose_up", lambda f, **k: _ok())
    seen = {}
    def fake_gate(*a, applied_ref=None, **k):
        seen["applied_ref"] = applied_ref
        return update.GATE_READY
    monkeypatch.setattr(update, "health_gate", fake_gate)
    assert _run(cf, monkeypatch) == engine.EXIT_OK
    assert seen["applied_ref"] == f"greffon/greffer@sha256:{'a' * 64}"
