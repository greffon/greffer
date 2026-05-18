"""Unit tests for greffer_cli.paths — config directory resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from greffer_cli import paths


def test_resolve_with_override(tmp_path: Path) -> None:
    result = paths.resolve_config_dir(str(tmp_path))
    assert result == tmp_path.resolve()


def test_resolve_override_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``~`` in ``--config-dir`` expands to $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    result = paths.resolve_config_dir("~/custom")
    assert result == (tmp_path / "custom").resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only default")
def test_default_uses_xdg_config_home_on_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Linux honors XDG_CONFIG_HOME when set."""
    monkeypatch.setattr(paths.sys, "platform", "linux", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = paths.default_config_dir()
    assert result == tmp_path / "greffer"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only default")
def test_default_falls_back_to_home_dotgreffer_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """macOS does not honor XDG; falls back to ``~/.greffer``."""
    monkeypatch.setattr(paths.sys, "platform", "darwin", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored"))
    result = paths.default_config_dir()
    assert result == tmp_path / ".greffer"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only default")
def test_default_falls_back_to_home_dotgreffer_when_no_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = paths.default_config_dir()
    assert result == tmp_path / ".greffer"


def test_default_windows_uses_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On Windows, default goes under %LOCALAPPDATA%."""
    monkeypatch.setattr(paths.sys, "platform", "win32", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = paths.default_config_dir()
    assert result == tmp_path / "greffer"


def test_env_env_path_is_under_config_dir(tmp_path: Path) -> None:
    assert paths.env_env_path(tmp_path) == tmp_path / "env.env"


def test_docker_compose_yml_path_is_under_config_dir(tmp_path: Path) -> None:
    assert paths.docker_compose_yml_path(tmp_path) == tmp_path / "docker-compose.yml"
