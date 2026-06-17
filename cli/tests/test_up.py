"""Unit tests for greffer_cli.up — env-value building + idempotence checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from greffer_cli import env_file, paths, up


# --- env-value building ---------------------------------------------

def test_build_env_values_tunnel_minimal() -> None:
    """Tunnel mode: --address is NOT required and not in the values."""
    values = up._build_env_values(
        manager_url="https://api.example.com",
        greffer_id="3d4f7760",
        mode="tunnel",
        address=None,
    )
    assert values["GREFFER_ID"] == "3d4f7760"
    assert values["GREFFON_BASE_SERVER"] == "https://api.example.com"
    assert values["GREFFER_MODE"] == "tunnel"
    assert "GREFFER_ADDRESS" not in values
    # GREFFER_PUBLIC_HOST is intentionally never written to env.env —
    # the greffer uses the manager-supplied URL (ports[].url) for
    # end-user-facing URLs, with PUBLIC_HOST only as a dev fallback.
    assert "GREFFER_PUBLIC_HOST" not in values


def test_build_env_values_proxy_includes_only_address() -> None:
    values = up._build_env_values(
        manager_url="https://api.example.com",
        greffer_id="3d4f7760",
        mode="proxy",
        address="mygreffer.example.com",
    )
    assert values["GREFFER_ADDRESS"] == "mygreffer.example.com"
    assert values["GREFFER_MODE"] == "proxy"
    # PUBLIC_HOST is never written — manager constructs end-user URLs.
    assert "GREFFER_PUBLIC_HOST" not in values


def test_build_env_values_proxy_requires_address() -> None:
    with pytest.raises(ValueError, match="proxy mode requires"):
        up._build_env_values(
            manager_url="https://api.example.com",
            greffer_id="3d4f7760",
            mode="proxy",
            address=None,
        )


def test_build_env_values_always_sets_critical_defaults() -> None:
    """GREFFER_WORKERS_ENABLED=true is critical — without it the
    greffer boots but never registers (lifespan.py:32-35)."""
    values = up._build_env_values(
        manager_url="https://api.example.com",
        greffer_id="abc",
        mode="tunnel",
        address=None,
    )
    assert values["GREFFER_WORKERS_ENABLED"] == "true"
    assert values["GREFFER_PORT"] == "8001"
    assert values["GREFFER_PROTOCOL"] == "https"
    assert values["GREFFON_PATH"] == "/data"


# --- write_config ----------------------------------------------------

def test_write_config_writes_both_files(tmp_path: Path) -> None:
    template = "version: '3.8'\nservices:\n  greffer:\n    image: ghcr.io/greffon/greffer:<TAG>\n"
    env_values = {"GREFFER_ID": "abc", "GREFFER_MODE": "tunnel"}
    up.write_config(tmp_path, template, "v1.2.3", env_values=env_values)
    compose_text = (tmp_path / "docker-compose.yml").read_text()
    assert "ghcr.io/greffon/greffer:v1.2.3" in compose_text
    assert "<TAG>" not in compose_text
    env = env_file.EnvFile.read(tmp_path / "env.env")
    assert env.get("GREFFER_ID") == "abc"
    assert env.get("GREFFER_MODE") == "tunnel"
    # The host config dir is recorded so remote update can mount it into the
    # updater; the greffer can't discover this host path on its own.
    assert env.get("GREFFER_HOST_CONFIG_DIR") == str(tmp_path.resolve())


def test_write_config_interpolates_image_tag(tmp_path: Path) -> None:
    """The <TAG> placeholder is replaced with cli/IMAGE_TAG's value."""
    template = "image: ghcr.io/greffon/greffer:<TAG> and nginx:<TAG>"
    up.write_config(tmp_path, template, "v0.4.2", env_values={"X": "1"})
    out = (tmp_path / "docker-compose.yml").read_text()
    assert out == "image: ghcr.io/greffon/greffer:v0.4.2 and nginx:v0.4.2"


# --- idempotence helpers --------------------------------------------

def test_already_initialized_for_returns_true_when_id_matches(tmp_path: Path) -> None:
    env = env_file.EnvFile.empty()
    env.set("GREFFER_ID", "abc")
    env.write_atomic(paths.env_env_path(tmp_path))
    assert up.already_initialized_for(tmp_path, "abc") is True


def test_already_initialized_for_returns_false_when_id_differs(tmp_path: Path) -> None:
    env = env_file.EnvFile.empty()
    env.set("GREFFER_ID", "abc")
    env.write_atomic(paths.env_env_path(tmp_path))
    assert up.already_initialized_for(tmp_path, "different") is False


def test_already_initialized_for_returns_false_when_no_env_env(tmp_path: Path) -> None:
    """Fresh host: no env.env yet → not initialized for anyone."""
    assert up.already_initialized_for(tmp_path, "abc") is False


def test_existing_greffer_id_surfaces_old_id_for_force_error_message(tmp_path: Path) -> None:
    """The init-refuse-on-mismatched-ID error needs the OLD id to tell
    the operator what to ask their admin to delete."""
    env = env_file.EnvFile.empty()
    env.set("GREFFER_ID", "old-uuid")
    env.write_atomic(paths.env_env_path(tmp_path))
    assert up.existing_greffer_id(tmp_path) == "old-uuid"


def test_existing_greffer_id_returns_none_on_fresh_host(tmp_path: Path) -> None:
    assert up.existing_greffer_id(tmp_path) is None
