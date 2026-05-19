"""Unit tests for up.run_state_machine — the state-machine driver.

We mock the Docker + manager edges (compose subprocesses, manager
state-public polling, httpx reachability probe) and verify the
driver walks the state transitions correctly and returns the right
exit code on each failure mode. Driver-internal behavior, NOT the
CLI glue (that's in test_main.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greffer_cli import compose, manager_client, paths, up


# --- helpers --------------------------------------------------------

def _ok(stdout: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stderr: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=returncode, stdout="", stderr=stderr)


def _patch_happy_path(
    monkeypatch: pytest.MonkeyPatch, *, mode: str = "tunnel", greffer_id: str = "abc",
) -> dict:
    """Wire every external edge to "everything works" responses.

    Returns the captured-args dict for tests that want to inspect
    what the driver called the edges with.
    """
    captured: dict = {"compose_up_calls": 0}

    # Containers report all-running on first probe → driver skips
    # `compose up` entirely (idempotent fast-path).
    monkeypatch.setattr(
        compose, "compose_services_running",
        lambda f, profile=None: {"greffer": True, "nginx": True}
        if mode == "proxy"
        else {"greffer": True, "nginx": True, "tunnel-sidecar": True},
    )
    monkeypatch.setattr(
        compose, "compose_up",
        lambda f, profile=None: (captured.update(compose_up_calls=captured["compose_up_calls"] + 1) or _ok()),
    )
    # Healthz from inside greffer: 200 first try.
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", lambda f: _ok())
    # Cert installed in nginx (proxy mode): file exists.
    monkeypatch.setattr(compose, "exec_nginx_cert_installed", lambda f: _ok())

    # Manager polls "GREFFER_REGISTERED" immediately.
    monkeypatch.setattr(
        manager_client, "poll_state",
        lambda *a, **k: iter([manager_client.StatePublic(state="GREFFER_REGISTERED")]),
    )

    # Reachability probe (proxy mode): clean 200 with matching id.
    monkeypatch.setattr(
        up, "reachability_self_test",
        lambda **k: ("ok", {}),
    )

    return captured


def _setup_compose_file(cfg: Path) -> None:
    cfg.mkdir(parents=True, exist_ok=True)
    paths.docker_compose_yml_path(cfg).write_text("# placeholder", encoding="utf-8")


# --- happy path -----------------------------------------------------

def test_run_state_machine_tunnel_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    _patch_happy_path(monkeypatch, mode="tunnel", greffer_id="abc-123")

    rc = up.run_state_machine(
        cfg, manager_url="https://m.example.com", greffer_id="abc-123",
        mode="tunnel", timeout=10.0,
    )
    assert rc == up.EXIT_OK
    out = capsys.readouterr().out
    assert "Starting" in out
    assert "Registering" in out
    assert "Awaiting cert" in out
    assert "Connected" in out
    # Tunnel-mode Connected message doesn't carry a public-host line.
    assert "tunnel" in out.lower()


def test_run_state_machine_proxy_happy_path_includes_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    _patch_happy_path(monkeypatch, mode="proxy", greffer_id="abc-123")

    rc = up.run_state_machine(
        cfg, manager_url="https://m.example.com", greffer_id="abc-123",
        mode="proxy", address="g.example.com", public_host="203.0.113.5",
        timeout=10.0,
    )
    assert rc == up.EXIT_OK
    out = capsys.readouterr().out
    assert "network reachable" in out  # REACHABILITY_OK
    assert "203.0.113.5" in out


def test_run_state_machine_fast_paths_when_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running on a Connected greffer must NOT call `compose up` again —
    that adds noise + 1-2s. The driver checks `compose_services_running`
    first and skips the up call when all services already report running."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    captured = _patch_happy_path(monkeypatch, mode="tunnel")

    up.run_state_machine(
        cfg, manager_url="https://m", greffer_id="abc",
        mode="tunnel", timeout=10.0,
    )
    assert captured["compose_up_calls"] == 0


# --- failure modes --------------------------------------------------

def test_run_state_machine_compose_up_failure_returns_distinct_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `docker compose up` itself fails (e.g. image pull error), the
    driver must surface a distinct exit code so operators get steered
    to the right hint."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    # Containers not running yet → driver will call compose_up.
    monkeypatch.setattr(compose, "compose_services_running", lambda f, profile=None: {})
    monkeypatch.setattr(
        compose, "compose_up",
        lambda f, profile=None: _fail(1, "Error response from daemon: pull access denied"),
    )

    rc = up.run_state_machine(
        cfg, manager_url="https://m", greffer_id="abc",
        mode="tunnel", timeout=10.0,
    )
    assert rc == up.EXIT_COMPOSE_UP_FAILED


def test_run_state_machine_starting_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If containers never come up, return EXIT_TIMEOUT_STARTING with the
    Starting-stuck hint string."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    # Containers list is empty forever — wait_for_compose_running times out.
    monkeypatch.setattr(compose, "compose_services_running", lambda f, profile=None: {})
    monkeypatch.setattr(compose, "compose_up", lambda f, profile=None: _ok())
    # Short timeout so the test runs fast. The poll_interval default
    # is 2s; with timeout=0.1 we hit one poll cycle and bail.
    monkeypatch.setattr(up.time, "sleep", lambda _: None)

    rc = up.run_state_machine(
        cfg, manager_url="https://m", greffer_id="abc",
        mode="tunnel", timeout=0.1,
    )
    assert rc == up.EXIT_TIMEOUT_STARTING


def test_run_state_machine_registering_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """If the manager never reaches GREFFER_REGISTERED (admin hasn't
    accepted), return EXIT_TIMEOUT_REGISTERING and print the
    accept-URL hint."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    monkeypatch.setattr(
        compose, "compose_services_running",
        lambda f, profile=None: {"greffer": True, "nginx": True, "tunnel-sidecar": True},
    )
    # Manager pegs on GREFFER_REGISTERING forever.
    monkeypatch.setattr(
        manager_client, "poll_state",
        lambda *a, **k: iter([
            manager_client.StatePublic(state="GREFFER_REGISTERING"),
        ] * 100),
    )
    monkeypatch.setattr(up.time, "sleep", lambda _: None)

    rc = up.run_state_machine(
        cfg, manager_url="https://m.example.com", greffer_id="abc-123",
        mode="tunnel", timeout=0.1,
    )
    assert rc == up.EXIT_TIMEOUT_REGISTERING
    out = capsys.readouterr().out + capsys.readouterr().err
    # The hint must include the accept URL operators need to send to admin.
    assert "register/accept/abc-123" in out


def test_run_state_machine_proxy_cert_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy mode + cert never appears in nginx → EXIT_TIMEOUT_AWAITING_CERT."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    monkeypatch.setattr(
        compose, "compose_services_running",
        lambda f, profile=None: {"greffer": True, "nginx": True},
    )
    monkeypatch.setattr(
        manager_client, "poll_state",
        lambda *a, **k: iter([manager_client.StatePublic(state="GREFFER_REGISTERED")]),
    )
    # Cert file: never exists.
    monkeypatch.setattr(compose, "exec_nginx_cert_installed", lambda f: _fail(1))
    monkeypatch.setattr(up.time, "sleep", lambda _: None)

    rc = up.run_state_machine(
        cfg, manager_url="https://m", greffer_id="abc",
        mode="proxy", address="g.example.com", public_host="203.0.113.5",
        timeout=0.1,
    )
    assert rc == up.EXIT_TIMEOUT_AWAITING_CERT


def test_run_state_machine_tunnel_skips_cert_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel mode: cert-installed signal is TBD per Stem HLD. Driver
    must NOT call exec_nginx_cert_installed — that probe is nginx-only
    and would always fail in a tunnel deployment (no nginx container)."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    _patch_happy_path(monkeypatch, mode="tunnel")

    cert_calls = {"n": 0}

    def _spy(f):
        cert_calls["n"] += 1
        return _fail(1)

    monkeypatch.setattr(compose, "exec_nginx_cert_installed", _spy)

    rc = up.run_state_machine(
        cfg, manager_url="https://m", greffer_id="abc",
        mode="tunnel", timeout=10.0,
    )
    assert rc == up.EXIT_OK
    assert cert_calls["n"] == 0  # never called in tunnel mode


# --- accept-URL formatting ------------------------------------------

def test_run_state_machine_accept_url_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The Registering message must include the exact accept-URL operators
    will paste to their admin. Manager URLs with trailing slashes must
    not produce double slashes; the URL path is
    /api/greffer/register/accept/<id>/."""
    cfg = tmp_path / ".greffer"
    _setup_compose_file(cfg)
    _patch_happy_path(monkeypatch, mode="tunnel")

    up.run_state_machine(
        cfg, manager_url="https://api.example.com/", greffer_id="uuid-here",
        mode="tunnel", timeout=10.0,
    )
    out = capsys.readouterr().out
    assert "https://api.example.com/api/greffer/register/accept/uuid-here/" in out
    # No double slashes from the manager_url stripping.
    assert "api.example.com//api" not in out
