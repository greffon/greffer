"""Tests for greffer_cli.update — the `greffer update` engine.

The compose layer is monkeypatched (no Docker) and ``sleep`` is stubbed
so the polling loops run instantly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greffer_cli import compose, update


def _ok(stdout: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail() -> compose.CommandResult:
    return compose.CommandResult(returncode=1, stdout="", stderr="boom")


_COMPOSE = """\
version: "3.8"
name: greffer
services:
  greffer:
    image: greffon/greffer:0.3.3
    volumes:
      - greffon-data:/data
  nginx:
    image: greffon/greffer-nginx:0.3.3
  tunnel-sidecar:
    image: greffon/tunnel-sidecar:0.3.3
    profiles: ["tunnel"]
volumes:
  greffon-data:
"""


# --- pure logic ------------------------------------------------------

def test_resolve_target_precedence() -> None:
    m = update.Manifest(latest="0.3.5")
    assert update.resolve_target(explicit_to="0.3.9", manifest=m) == "0.3.9"
    assert update.resolve_target(explicit_to=None, manifest=m) == "0.3.5"
    assert update.resolve_target(explicit_to=None, manifest=None) is None
    assert update.resolve_target(explicit_to=None, manifest=update.Manifest()) is None


def test_no_rollback_blocked() -> None:
    m = update.Manifest(no_rollback_from=["0.3.3->0.3.4"])
    assert update.no_rollback_blocked(m, "0.3.3", "0.3.4") is True   # listed
    assert update.no_rollback_blocked(m, "0.3.3", "0.3.5") is False  # reachable, absent => safe
    assert update.no_rollback_blocked(None, "0.3.3", "0.3.4") is None  # unreachable
    assert update.no_rollback_blocked(m, None, "0.3.4") is None        # current unknown


def test_parse_readyz() -> None:
    ready = update.parse_readyz(_ok(json.dumps({"id": "g1", "status": "ready", "reasons": []})))
    assert ready.ok and ready.id == "g1" and ready.status == "ready"
    deg = update.parse_readyz(_ok(json.dumps({"id": "g1", "status": "degraded", "reasons": ["registration_pending"]})))
    assert deg.status == "degraded" and deg.reasons == ["registration_pending"]
    bad = update.parse_readyz(_fail())
    assert not bad.ok
    assert not update.parse_readyz(_ok("not json")).ok


def test_active_services() -> None:
    assert update.active_services("proxy") == ["greffer", "nginx"]
    assert update.active_services("tunnel") == ["greffer", "nginx", "tunnel-sidecar"]


def test_fetch_manifest_rejects_plaintext() -> None:
    assert update.fetch_manifest("http://greffon.io/x.json") is None


# --- FakeDocker harness ----------------------------------------------

class FakeDocker:
    def __init__(self) -> None:
        self.running_image = "sha256:OLD"
        self.target_image = "sha256:NEW"
        self.readyz = {"id": "g1", "status": "ready", "reasons": []}
        self.pull_ok = True
        self.up_ok = True
        self.services_up = True
        self.restart = 0
        self.healthz_ok = True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(compose, "exec_greffer_version", lambda f: "0.3.3")
        monkeypatch.setattr(compose, "container_image_id", lambda f, s: self.running_image)
        monkeypatch.setattr(compose, "image_id", lambda ref: self.target_image)
        monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
        monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: self.restart)
        monkeypatch.setattr(
            compose, "exec_in_greffer_healthz",
            lambda f: _ok() if self.healthz_ok else _fail(),
        )
        monkeypatch.setattr(
            compose, "exec_in_greffer_readyz",
            lambda f: _ok(json.dumps(self.readyz)),
        )
        monkeypatch.setattr(
            compose, "compose_services_running",
            lambda f, *, profile=None: {
                s: self.services_up for s in ("greffer", "nginx", "tunnel-sidecar")
            },
        )
        monkeypatch.setattr(
            compose, "compose_pull",
            lambda f, *, profile=None, services=None: _ok() if self.pull_ok else _fail(),
        )

        def fake_up(f, *, profile=None):
            # a successful recreate advances the running image to the target
            if self.up_ok:
                self.running_image = self.target_image
                return _ok()
            return _fail()

        monkeypatch.setattr(compose, "compose_up", fake_up)
        monkeypatch.setattr(
            update, "fetch_manifest", lambda url, timeout=10.0: update.Manifest(latest="0.3.4"),
        )


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text(_COMPOSE, encoding="utf-8")
    (tmp_path / "env.env").write_text(
        'GREFFER_ID="g1"\nGREFFER_MODE="proxy"\n', encoding="utf-8",
    )
    return tmp_path


def _run(cfg: Path, **kw) -> int:
    kw.setdefault("sleep", lambda _s: None)
    return update.run_update(cfg, **kw)


# --- engine paths ----------------------------------------------------

def test_happy_path(cfg: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeDocker().install(monkeypatch)
    rc = _run(cfg, target="0.3.4")
    assert rc == update.EXIT_OK
    assert "Updated to 0.3.4" in capsys.readouterr().out
    # compose file was retagged to the target
    assert "greffon/greffer:0.3.4" in (cfg / "docker-compose.yml").read_text()


def test_check_only_changes_nothing(cfg: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeDocker().install(monkeypatch)
    before = (cfg / "docker-compose.yml").read_text()
    rc = _run(cfg, target="0.3.4", check_only=True)
    assert rc == update.EXIT_OK
    assert (cfg / "docker-compose.yml").read_text() == before  # untouched
    assert "no changes made" in capsys.readouterr().out


def test_idempotent_when_already_target(cfg: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    fd = FakeDocker()
    fd.running_image = "sha256:SAME"
    fd.target_image = "sha256:SAME"  # running already == target
    fd.install(monkeypatch)
    rc = _run(cfg, target="0.3.4")
    assert rc == update.EXIT_OK
    assert "Already up to date" in capsys.readouterr().out


def test_preflight_refuses_without_named_data_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # /data is a bind mount, not a named volume
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  greffer:\n    image: greffon/greffer:0.3.3\n"
        "    volumes:\n      - /srv/greffer:/data\n",
        encoding="utf-8",
    )
    (tmp_path / "env.env").write_text('GREFFER_ID="g1"\nGREFFER_MODE="proxy"\n', encoding="utf-8")
    FakeDocker().install(monkeypatch)
    assert _run(tmp_path, target="0.3.4") == update.EXIT_PREFLIGHT_REFUSED


def test_pull_failure_restores_compose_and_reports(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fd = FakeDocker()
    fd.pull_ok = False
    fd.install(monkeypatch)
    rc = _run(cfg, target="0.3.4")
    assert rc == update.EXIT_FAILED_ROLLED_BACK
    # compose restored to the prior tag (no recreate happened)
    text = (cfg / "docker-compose.yml").read_text()
    assert "greffon/greffer:0.3.3" in text
    assert "greffon/greffer:0.3.4" not in text


def test_gate_fatal_rolls_back(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fd = FakeDocker()
    fd.install(monkeypatch)
    # After recreate, /readyz reports fatal; rollback then comes up healthy.
    calls = {"n": 0}
    orig_readyz = {"fatal": {"id": "g1", "status": "fatal", "reasons": ["docker_unreachable"]},
                   "ready": {"id": "g1", "status": "ready", "reasons": []}}

    def staged_readyz(f):
        # first poll: fatal (the update); after rollback recreate: ready
        return _ok(json.dumps(orig_readyz["ready" if fd.running_image == "sha256:OLD" else "fatal"]))

    # rollback restores old image (set_image_refs writes prior tag; we model
    # the running image flipping back to OLD on the rollback compose_up).
    def fake_up(f, *, profile=None):
        # forward recreate -> NEW; rollback recreate -> OLD (refs restored)
        text = (cfg / "docker-compose.yml").read_text()
        fd.running_image = "sha256:NEW" if "greffon/greffer:0.3.4" in text else "sha256:OLD"
        return _ok()

    monkeypatch.setattr(compose, "compose_up", fake_up)
    monkeypatch.setattr(compose, "exec_in_greffer_readyz", staged_readyz)
    rc = _run(cfg, target="0.3.4")
    assert rc == update.EXIT_FAILED_ROLLED_BACK
    # ended back on the prior tag
    assert "greffon/greffer:0.3.3" in (cfg / "docker-compose.yml").read_text()


def test_needs_confirm_when_rollback_unknown(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fd = FakeDocker()
    fd.install(monkeypatch)
    # manifest unreachable => no_rollback status unknown => needs --confirm
    monkeypatch.setattr(update, "fetch_manifest", lambda url, timeout=10.0: None)
    assert _run(cfg, target="0.3.4") == update.EXIT_PREFLIGHT_REFUSED
    assert _run(cfg, target="0.3.4", confirm_no_rollback=True) == update.EXIT_OK


def test_health_gate_waits_through_registration_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    seq = [
        {"id": "g1", "status": "degraded", "reasons": ["registration_pending"]},
        {"id": "g1", "status": "ready", "reasons": []},
    ]
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _ok())
    monkeypatch.setattr(compose, "exec_in_greffer_readyz", lambda f: _ok(json.dumps(seq.pop(0) if seq else seq[-1])))
    monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
    monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: 0)
    monkeypatch.setattr(compose, "container_image_id", lambda f, s: "sha256:X")
    monkeypatch.setattr(compose, "image_id", lambda ref: "sha256:X")
    monkeypatch.setattr(compose, "compose_services_running", lambda f, *, profile=None: {"greffer": True, "nginx": True})
    outcome = update.health_gate(
        f, greffer_id="g1", target="0.3.4", services=["greffer", "nginx"],
        profile=None, timeout=10.0, poll_interval=0.0, sleep=lambda _s: None,
    )
    assert outcome == update.GATE_READY


def test_health_gate_version_not_applied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _ok())
    monkeypatch.setattr(compose, "exec_in_greffer_readyz", lambda f: _ok(json.dumps({"id": "g1", "status": "ready", "reasons": []})))
    monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
    monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: 0)
    monkeypatch.setattr(compose, "container_image_id", lambda f, s: "sha256:OLD")
    monkeypatch.setattr(compose, "image_id", lambda ref: "sha256:NEW")  # target != running
    outcome = update.health_gate(
        f, greffer_id="g1", target="0.3.4", services=["greffer"],
        profile=None, timeout=10.0, sleep=lambda _s: None,
    )
    assert outcome == update.GATE_NOT_APPLIED
