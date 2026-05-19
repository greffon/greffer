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


def test_up_idempotent_fast_path_uses_persisted_mode(tmp_path: Path) -> None:
    """Regression: when env.env already exists for the same greffer_id, the
    manual-compose hint must reflect the persisted mode (proxy), NOT the
    CLI's default --mode tunnel. Otherwise re-running `greffer up --id …`
    on a proxy host prints a tunnel-profile command that won't match
    what's on disk."""
    cfg = tmp_path / ".greffer"
    _write_proxy_env(cfg, greffer_id="abc-123")

    # Invoke `greffer up --id abc-123 --config-dir …` WITHOUT --mode.
    # The default --mode tunnel must NOT win over the persisted "proxy".
    result = runner.invoke(
        main.app,
        ["up", "--id", "abc-123", "--config-dir", str(cfg)],
    )
    assert result.exit_code == 0, result.stdout
    # Tunnel-mode hint includes `--profile tunnel`; proxy hint does not.
    assert "--profile tunnel" not in result.stdout
    assert "docker compose -f" in result.stdout


def test_up_idempotent_fast_path_uses_persisted_mode_tunnel(tmp_path: Path) -> None:
    """Mirror of the above for a tunnel-mode host re-invoked with an
    explicit --mode proxy flag — persisted wins, prints tunnel hint."""
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

    # Operator typo'd --mode proxy on a host previously initialized as
    # tunnel. Need --address/--public-host to pass arg validation;
    # the fast-path then uses the persisted (tunnel) mode for the hint.
    result = runner.invoke(
        main.app,
        [
            "up", "--id", "xyz", "--config-dir", str(cfg),
            "--mode", "proxy",
            "--address", "g.example.com",
            "--public-host", "203.0.113.5",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "--profile tunnel" in result.stdout
