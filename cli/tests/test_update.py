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
    before = (cfg / "docker-compose.yml").read_text()
    rc = _run(cfg, target="0.3.4")
    assert rc == update.EXIT_OK
    assert "Already up to date" in capsys.readouterr().out
    # the short-circuit must return BEFORE set_image_tag; file untouched
    text = (cfg / "docker-compose.yml").read_text()
    assert text == before
    assert "greffon/greffer:0.3.3" in text and "greffon/greffer:0.3.4" not in text


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


# --- health-gate failure outcomes ------------------------------------

def _gate_with_readyz(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, readyz: dict) -> str:
    """Run health_gate with a fixed /readyz body and healthz live."""
    f = tmp_path / "c.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _ok())
    monkeypatch.setattr(compose, "exec_in_greffer_readyz", lambda f: _ok(json.dumps(readyz)))
    monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
    monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: 0)
    # version-applied helpers (only reached on a "ready" status)
    monkeypatch.setattr(compose, "container_image_id", lambda f, s: "sha256:X")
    monkeypatch.setattr(compose, "image_id", lambda ref: "sha256:X")
    monkeypatch.setattr(compose, "compose_services_running", lambda f, *, profile=None: {"greffer": True})
    return update.health_gate(
        f, greffer_id="g1", target="0.3.4", services=["greffer"],
        profile=None, timeout=10.0, poll_interval=0.0, sleep=lambda _s: None,
    )


def test_health_gate_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = _gate_with_readyz(
        monkeypatch, tmp_path,
        {"id": "g1", "status": "fatal", "reasons": ["docker_unreachable"]},
    )
    assert outcome == update.GATE_FATAL


def test_health_gate_wrong_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = _gate_with_readyz(
        monkeypatch, tmp_path,
        {"id": "someone-else", "status": "ready", "reasons": []},
    )
    assert outcome == update.GATE_WRONG_ID


def test_health_gate_degraded_other_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a degraded reason outside the tolerated set fails (not registration_pending)
    outcome = _gate_with_readyz(
        monkeypatch, tmp_path,
        {"id": "g1", "status": "degraded", "reasons": ["disk_full"]},
    )
    assert outcome == update.GATE_DEGRADED_OTHER


def test_health_gate_degraded_mixed_reasons_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # registration_pending alone is tolerated, but paired with another reason
    # the gate must still fail (set-membership, not exact-list equality).
    outcome = _gate_with_readyz(
        monkeypatch, tmp_path,
        {"id": "g1", "status": "degraded", "reasons": ["registration_pending", "disk_full"]},
    )
    assert outcome == update.GATE_DEGRADED_OTHER


def test_health_gate_crash_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    # never live, so the gate falls through to the crash-loop check
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _fail())
    monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
    counts = iter([0, 2, 2, 2])  # baseline 0, then climbed by 2 -> crash-loop
    monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: next(counts))
    outcome = update.health_gate(
        f, greffer_id="g1", target="0.3.4", services=["greffer"],
        profile=None, timeout=10.0, poll_interval=0.0, sleep=lambda _s: None,
    )
    assert outcome == update.GATE_CRASH_LOOP


def test_health_gate_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    # drive the monotonic clock: deadline then one in-window tick, then past it
    times = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(update.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _fail())
    monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
    monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: 0)
    outcome = update.health_gate(
        f, greffer_id="g1", target="0.3.4", services=["greffer"],
        profile=None, timeout=5.0, poll_interval=0.0, sleep=lambda _s: None,
    )
    assert outcome == update.GATE_TIMEOUT


# --- rollback that itself fails (EXIT_FAILED_ROLLBACK_FAILED) ---------

def test_rollback_compose_up_fails_exits_2(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDocker().install(monkeypatch)
    # gate sees fatal -> rollback; the rollback `up` (2nd compose_up) fails
    monkeypatch.setattr(
        compose, "exec_in_greffer_readyz",
        lambda f: _ok(json.dumps({"id": "g1", "status": "fatal", "reasons": ["docker_unreachable"]})),
    )
    calls = {"n": 0}

    def up(f, *, profile=None):
        calls["n"] += 1
        return _ok() if calls["n"] == 1 else _fail()  # forward ok, rollback fails

    monkeypatch.setattr(compose, "compose_up", up)
    assert _run(cfg, target="0.3.4") == update.EXIT_FAILED_ROLLBACK_FAILED


def test_rollback_health_never_ready_exits_2(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDocker().install(monkeypatch)
    # gate fatal -> rollback recreate succeeds but never reaches /readyz ready
    monkeypatch.setattr(
        compose, "exec_in_greffer_readyz",
        lambda f: _ok(json.dumps({"id": "g1", "status": "fatal", "reasons": ["docker_unreachable"]})),
    )
    monkeypatch.setattr(update, "_rollback_health", lambda *a, **k: False)
    assert _run(cfg, target="0.3.4") == update.EXIT_FAILED_ROLLBACK_FAILED


# --- mode resolution + id pre-flight ---------------------------------

def test_resolve_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from greffer_cli import env_file
    f = tmp_path / "docker-compose.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    # explicit GREFFER_MODE always wins, no detection
    env = env_file.EnvFile.from_text('GREFFER_MODE="proxy"\n')
    assert update._resolve_mode(env, f) == "proxy"
    # missing -> detect tunnel when the sidecar has a container
    monkeypatch.setattr(
        compose, "compose_services_running",
        lambda cf, *, profile=None: {"greffer": True, "tunnel-sidecar": True},
    )
    assert update._resolve_mode(env_file.EnvFile.from_text('GREFFER_ID="g1"\n'), f) == "tunnel"
    # missing -> proxy when no sidecar container is present
    monkeypatch.setattr(
        compose, "compose_services_running",
        lambda cf, *, profile=None: {"greffer": True, "nginx": True},
    )
    assert update._resolve_mode(env_file.EnvFile.from_text('GREFFER_ID="g1"\n'), f) == "proxy"


def test_preflight_refuses_without_greffer_id(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (cfg / "env.env").write_text('GREFFER_MODE="proxy"\n', encoding="utf-8")  # no GREFFER_ID
    FakeDocker().install(monkeypatch)
    assert _run(cfg, target="0.3.4") == update.EXIT_PREFLIGHT_REFUSED


# --- target / manifest hardening (tag injection) ---------------------

def test_resolve_target_rejects_invalid_tag() -> None:
    # a tampered manifest naming a tag with injected YAML / newline is dropped
    evil = update.Manifest(latest='latest\n    command: ["sh","-c","x"]')
    assert update.resolve_target(explicit_to=None, manifest=evil) is None
    # a non-string manifest latest never stringifies into a ref
    assert update.resolve_target(explicit_to=None, manifest=update.Manifest(latest=None)) is None
    # an invalid explicit --to is rejected too
    assert update.resolve_target(explicit_to="bad:tag", manifest=None) is None
    # a clean tag still resolves
    assert update.resolve_target(explicit_to="0.3.4", manifest=None) == "0.3.4"


class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def test_fetch_manifest_coerces_and_tolerates_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, str] = {}
    monkeypatch.setattr(
        update.urllib.request, "urlopen",
        lambda url, timeout=None: _FakeResp(holder["body"]),
    )
    # non-string latest (a list) is coerced to None, not stringified into a ref
    holder["body"] = json.dumps({"latest": ["a", "b"]})
    assert update.fetch_manifest("https://x/m.json").latest is None
    # malformed JSON body -> None manifest (treated as unreachable)
    holder["body"] = "not json {"
    assert update.fetch_manifest("https://x/m.json") is None
    # a non-object JSON body -> None manifest
    holder["body"] = "[]"
    assert update.fetch_manifest("https://x/m.json") is None
    # a well-formed manifest still parses
    holder["body"] = json.dumps({"latest": "0.3.5", "no_rollback_from": ["a->b"]})
    m = update.fetch_manifest("https://x/m.json")
    assert m.latest == "0.3.5" and m.no_rollback_from == ["a->b"]


def test_parse_readyz_defensive_shapes() -> None:
    # JSON that isn't an object -> not ok
    assert update.parse_readyz(_ok("[]")).ok is False
    assert update.parse_readyz(_ok("5")).ok is False
    # reasons present but not a list -> coerced to [] (guards char-iteration
    # of a scalar into the degraded-reason check)
    scalar = update.parse_readyz(_ok(json.dumps({"status": "degraded", "reasons": "oops"})))
    assert scalar.ok is True and scalar.status == "degraded" and scalar.reasons == []


# --- rollback-safety gate: listed (True) branch ----------------------

def test_needs_confirm_when_rollback_listed(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDocker().install(monkeypatch)
    # manifest reachable AND the current->target pair is flagged no-rollback
    monkeypatch.setattr(
        update, "fetch_manifest",
        lambda url, timeout=10.0: update.Manifest(latest="0.3.4", no_rollback_from=["0.3.3->0.3.4"]),
    )
    assert _run(cfg, target="0.3.4") == update.EXIT_PREFLIGHT_REFUSED
    assert _run(cfg, target="0.3.4", confirm_no_rollback=True) == update.EXIT_OK


# --- tunnel mode + partial-services-up -------------------------------

@pytest.fixture
def tunnel_cfg(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text(_COMPOSE, encoding="utf-8")
    (tmp_path / "env.env").write_text(
        'GREFFER_ID="g1"\nGREFFER_MODE="tunnel"\n', encoding="utf-8",
    )
    return tmp_path


def test_tunnel_mode_recreates_with_tunnel_profile(tunnel_cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fd = FakeDocker()
    fd.install(monkeypatch)
    rec: dict[str, object] = {}

    def up(f, *, profile=None):
        rec["up_profile"] = profile
        fd.running_image = fd.target_image
        return _ok()

    monkeypatch.setattr(compose, "compose_up", up)
    # the gate must see the sidecar up in tunnel mode
    monkeypatch.setattr(
        compose, "compose_services_running",
        lambda f, *, profile=None: {"greffer": True, "nginx": True, "tunnel-sidecar": True},
    )
    assert _run(tunnel_cfg, target="0.3.4") == update.EXIT_OK
    assert rec["up_profile"] == "tunnel"  # recreate ran under --profile tunnel


def test_partial_services_up_rolls_back(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fd = FakeDocker()
    fd.install(monkeypatch)
    # greffer is ready + version-applied, but nginx never comes up while on
    # the target -> gate can't reach READY -> times out -> rollback. nginx
    # comes back once the refs are restored, so the rollback is healthy.
    def services_running(f, *, profile=None):
        on_target = "greffon/greffer:0.3.4" in (cfg / "docker-compose.yml").read_text()
        return {"greffer": True, "nginx": not on_target}

    monkeypatch.setattr(compose, "compose_services_running", services_running)
    rc = _run(cfg, target="0.3.4", timeout=0.1)
    assert rc == update.EXIT_FAILED_ROLLED_BACK
    text = (cfg / "docker-compose.yml").read_text()
    assert "greffon/greffer:0.3.3" in text and "greffon/greffer:0.3.4" not in text


def test_health_gate_single_restart_tolerated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text(_COMPOSE, encoding="utf-8")
    seq = [
        {"id": "g1", "status": "degraded", "reasons": ["registration_pending"]},
        {"id": "g1", "status": "ready", "reasons": []},
    ]
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _ok())
    monkeypatch.setattr(
        compose, "exec_in_greffer_readyz",
        lambda f: _ok(json.dumps(seq.pop(0) if seq else {"id": "g1", "status": "ready", "reasons": []})),
    )
    monkeypatch.setattr(compose, "service_container_id", lambda f, s: "cid")
    counts = iter([0, 1, 1, 1])  # baseline 0, then a single restart (delta 1, tolerated)
    monkeypatch.setattr(compose, "docker_inspect_restart_count", lambda c: next(counts))
    monkeypatch.setattr(compose, "container_image_id", lambda f, s: "sha256:X")
    monkeypatch.setattr(compose, "image_id", lambda ref: "sha256:X")
    monkeypatch.setattr(compose, "compose_services_running", lambda f, *, profile=None: {"greffer": True})
    outcome = update.health_gate(
        f, greffer_id="g1", target="0.3.4", services=["greffer"],
        profile=None, timeout=10.0, poll_interval=0.0, sleep=lambda _s: None,
    )
    assert outcome == update.GATE_READY  # one restart is tolerated; boundary is strictly > 1


# --- interrupt safety + concurrency lock -----------------------------

def test_interrupt_after_retag_disarms_compose(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDocker().install(monkeypatch)

    def boom(f, *, profile=None):
        raise KeyboardInterrupt  # operator Ctrl-C mid-recreate (after retag)

    monkeypatch.setattr(compose, "compose_up", boom)
    with pytest.raises(KeyboardInterrupt):
        _run(cfg, target="0.3.4")
    # the finally-disarm must have restored the prior refs so a later bare
    # `docker compose up` can't recreate into the un-gated target
    text = (cfg / "docker-compose.yml").read_text()
    assert "greffon/greffer:0.3.3" in text and "greffon/greffer:0.3.4" not in text


def test_update_refused_when_lock_held(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fcntl = pytest.importorskip("fcntl")  # POSIX-only; skip on Windows
    FakeDocker().install(monkeypatch)
    # set_image_tag must NOT run while another process holds the lock
    tagged = {"called": False}
    real_set = compose.set_image_tag
    monkeypatch.setattr(
        compose, "set_image_tag",
        lambda f, t: (tagged.__setitem__("called", True), real_set(f, t))[1],
    )
    held = open(cfg / ".update.lock", "w", encoding="utf-8")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert _run(cfg, target="0.3.4") == update.EXIT_PREFLIGHT_REFUSED
        assert tagged["called"] is False
    finally:
        held.close()


def test_pull_validates_all_images_in_tunnel_profile(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even in proxy mode (the cfg fixture), the pull must validate ALL node
    # images under the tunnel profile (set_image_tag retagged the sidecar
    # line too), so a bad sidecar tag is caught now, not on a later switch.
    fd = FakeDocker()
    fd.install(monkeypatch)
    seen: dict[str, object] = {}

    def rec_pull(f, *, profile=None, services=None):
        seen["profile"] = profile
        seen["services"] = services
        return _ok()

    monkeypatch.setattr(compose, "compose_pull", rec_pull)
    assert _run(cfg, target="0.3.4") == update.EXIT_OK
    assert seen["profile"] == "tunnel"                        # not the proxy profile
    assert set(seen["services"]) == set(update.SERVICE_REPO)  # all three, incl. sidecar


def test_sigterm_after_retag_disarms_and_exits_143(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fcntl")  # POSIX signal path
    import os
    import signal

    FakeDocker().install(monkeypatch)

    def kill_self(f, *, profile=None):
        os.kill(os.getpid(), signal.SIGTERM)  # bare kill mid-recreate
        return _ok()  # not reached; the installed handler raises first

    monkeypatch.setattr(compose, "compose_up", kill_self)
    prev = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as ei:
        _run(cfg, target="0.3.4")
    assert ei.value.code == 143  # killed before the gate passed
    text = (cfg / "docker-compose.yml").read_text()
    assert "greffon/greffer:0.3.3" in text and "greffon/greffer:0.3.4" not in text
    assert signal.getsignal(signal.SIGTERM) is prev  # _restore_sigterm ran in finally


def test_bad_manifest_message_distinct_from_unreachable(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    FakeDocker().install(monkeypatch)
    # manifest reachable but its `latest` is unusable (coerced to None)
    monkeypatch.setattr(update, "fetch_manifest", lambda url, timeout=10.0: update.Manifest(latest=None))
    assert _run(cfg) == update.EXIT_PREFLIGHT_REFUSED  # no --to -> auto-latest
    err = capsys.readouterr().err
    assert "did not name a usable latest" in err  # UPDATE_BAD_MANIFEST, not "unreachable"
