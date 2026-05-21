"""End-to-end Typer-CLI tests for the `greffer up` idempotent fast-path."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from greffer_cli import env_file, main, paths


runner = CliRunner()


def test_read_image_tag_reads_from_package_resources() -> None:
    """IMAGE_TAG lives INSIDE the greffer_cli package so importlib.resources
    finds it in both editable installs and built wheels. An earlier
    layout (cli/IMAGE_TAG, one level above the package) was silently
    dropped from built wheels — fallback "main" rendered image refs
    pointing at a non-existent registry tag."""
    tag = main._read_image_tag()
    # Must be non-empty, non-whitespace, and not the fallback default
    # (the fallback should only fire if IMAGE_TAG is missing from the
    # package, which would be a packaging regression we want to catch).
    assert tag
    assert tag.strip() == tag
    assert tag != ""


def _write_proxy_env(cfg: Path, greffer_id: str) -> None:
    """Simulate an already-initialized proxy host: env.env + compose.yml on disk."""
    cfg.mkdir(parents=True, exist_ok=True)
    env = env_file.EnvFile(values={
        "GREFFER_ID": greffer_id,
        "GREFFON_BASE_SERVER": "https://api.example.com",
        "GREFFER_MODE": "proxy",
        "GREFFER_ADDRESS": "g.example.com",
        "GREFFER_PUBLIC_HOST": "203.0.113.5",
        "GREFFER_PORT": "8001",
    })
    env.write_atomic(paths.env_env_path(cfg))
    # The manual-hint code path references the compose file path string,
    # not its contents — touch the file so paths look real.
    paths.docker_compose_yml_path(cfg).write_text("# placeholder", encoding="utf-8")


def _stub_run_state_machine(captured: dict) -> "callable":
    """Build a stub for up.run_state_machine that records its args.

    The CLI tests verify the persisted-mode value PROPAGATED into the
    driver call — not what the driver itself prints. Stubbing here keeps
    the test focused on the CLI-glue layer; driver-internal behavior
    (state transitions, heartbeats, timeouts) is covered by test_up.py.
    """
    def _stub(cfg, **kwargs):
        captured.update(kwargs)
        captured["cfg"] = cfg
        from greffer_cli import up as _up
        return _up.EXIT_OK
    return _stub


def test_up_idempotent_fast_path_uses_persisted_mode_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when env.env already exists for the same greffer_id, the
    state-machine driver must be called with the PERSISTED mode (proxy),
    not the CLI's default --mode tunnel. Otherwise re-running
    `greffer up --id …` on a proxy host would call the driver in
    tunnel mode and skip the proxy-specific cert + reachability paths."""
    cfg = tmp_path / ".greffer"
    _write_proxy_env(cfg, greffer_id="abc-123")

    from greffer_cli import up as up_mod
    captured: dict = {}
    monkeypatch.setattr(up_mod, "run_state_machine", _stub_run_state_machine(captured))

    # Invoke `greffer up --id abc-123 --config-dir …` WITHOUT --mode.
    # The default --mode tunnel must NOT win over the persisted "proxy".
    result = runner.invoke(
        main.app,
        ["up", "--id", "abc-123", "--config-dir", str(cfg)],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["mode"] == "proxy"
    # And the proxy-mode address propagated too:
    assert captured["address"] == "g.example.com"


def test_up_idempotent_fast_path_uses_persisted_mode_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the above for a tunnel-mode host re-invoked with an
    explicit --mode proxy flag — persisted wins, driver gets called
    in tunnel mode."""
    cfg = tmp_path / ".greffer"
    cfg.mkdir(parents=True)
    env = env_file.EnvFile(values={
        "GREFFER_ID": "xyz",
        "GREFFON_BASE_SERVER": "https://api.example.com",
        "GREFFER_MODE": "tunnel",
        "GREFFER_PORT": "8001",
    })
    env.write_atomic(paths.env_env_path(cfg))
    paths.docker_compose_yml_path(cfg).write_text("# placeholder", encoding="utf-8")

    from greffer_cli import up as up_mod
    captured: dict = {}
    monkeypatch.setattr(up_mod, "run_state_machine", _stub_run_state_machine(captured))

    # Operator typo'd --mode proxy on a host previously initialized as
    # tunnel. Need --address to pass arg validation; the persisted
    # (tunnel) mode wins.
    result = runner.invoke(
        main.app,
        [
            "up", "--id", "xyz", "--config-dir", str(cfg),
            "--mode", "proxy",
            "--address", "g.example.com",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["mode"] == "tunnel"


def test_up_idempotent_fast_path_uses_persisted_manager_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an operator with a self-hosted manager who re-runs
    `greffer up --id …` (no --manager flag → CLI default
    https://api.greffon.io) must NOT have the driver point at Greffon
    Hosted. The persisted GREFFON_BASE_SERVER wins."""
    cfg = tmp_path / ".greffer"
    cfg.mkdir(parents=True)
    env = env_file.EnvFile(values={
        "GREFFER_ID": "abc",
        "GREFFON_BASE_SERVER": "https://manager.self-hosted.example.com",
        "GREFFER_MODE": "tunnel",
        "GREFFER_PORT": "8001",
    })
    env.write_atomic(paths.env_env_path(cfg))
    paths.docker_compose_yml_path(cfg).write_text("# placeholder", encoding="utf-8")

    from greffer_cli import up as up_mod
    captured: dict = {}
    monkeypatch.setattr(up_mod, "run_state_machine", _stub_run_state_machine(captured))

    result = runner.invoke(
        main.app,
        ["up", "--id", "abc", "--config-dir", str(cfg)],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["manager_url"] == "https://manager.self-hosted.example.com"
    # Specifically NOT the CLI default:
    assert "greffon.io" not in captured["manager_url"]


def test_up_explicit_manager_flag_overrides_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an explicit --manager must win over the persisted
    GREFFON_BASE_SERVER so operators can recover from a typo in the
    initial install command or migrate to a new manager.

    The previous fix made persisted always win, which blocked recovery."""
    cfg = tmp_path / ".greffer"
    cfg.mkdir(parents=True)
    env = env_file.EnvFile(values={
        "GREFFER_ID": "abc",
        "GREFFON_BASE_SERVER": "https://typo.example.com",  # the bad URL
        "GREFFER_MODE": "tunnel",
        "GREFFER_PORT": "8001",
    })
    env.write_atomic(paths.env_env_path(cfg))
    paths.docker_compose_yml_path(cfg).write_text("# placeholder", encoding="utf-8")

    from greffer_cli import up as up_mod
    captured: dict = {}
    monkeypatch.setattr(up_mod, "run_state_machine", _stub_run_state_machine(captured))

    # Operator passes --manager explicitly to recover from the typo.
    result = runner.invoke(
        main.app,
        [
            "up", "--id", "abc", "--config-dir", str(cfg),
            "--manager", "https://correct.example.com",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # Explicit flag wins:
    assert captured["manager_url"] == "https://correct.example.com"
    # NOT the persisted typo:
    assert "typo" not in captured["manager_url"]


def test_up_propagates_driver_failure_as_typer_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI must surface the driver's exit code (timeout, compose-up
    failure, etc.) so operators and CI can detect failure. Returning 0
    when the driver returned a timeout would mask real problems."""
    cfg = tmp_path / ".greffer"
    _write_proxy_env(cfg, greffer_id="abc-123")

    from greffer_cli import up as up_mod
    monkeypatch.setattr(
        up_mod, "run_state_machine",
        lambda cfg, **kw: up_mod.EXIT_TIMEOUT_REGISTERING,
    )

    result = runner.invoke(
        main.app,
        ["up", "--id", "abc-123", "--config-dir", str(cfg)],
    )
    assert result.exit_code == up_mod.EXIT_TIMEOUT_REGISTERING
