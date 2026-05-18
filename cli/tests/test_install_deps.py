"""Unit tests for greffer_cli.install_deps — version parsing + detect logic."""

from __future__ import annotations

import pytest

from greffer_cli import compose, install_deps


def _ok(stdout: str) -> compose.CommandResult:
    return compose.CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail() -> compose.CommandResult:
    return compose.CommandResult(returncode=127, stdout="", stderr="command not found")


# --- _short_version: plain-text parser ------------------------------

def test_short_version_standard_output() -> None:
    assert install_deps._short_version("Docker version 25.0.3, build abc123") == "25.0.3"


def test_short_version_no_build_suffix() -> None:
    """Some distros / older docker print without the trailing ", build …" segment."""
    assert install_deps._short_version("Docker version 24.0.7") == "24.0.7"


def test_short_version_trailing_whitespace() -> None:
    assert install_deps._short_version("  Docker version 25.0.3, build abc\n") == "25.0.3"


def test_short_version_unrecognized_falls_back() -> None:
    """If Docker ever changes its output format we degrade gracefully."""
    assert install_deps._short_version("podman 4.5.0") == "?"
    assert install_deps._short_version("") == "?"


# --- detect_and_instruct entry point --------------------------------

def test_detect_and_instruct_docker_present_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        compose, "docker_cli_installed",
        lambda: _ok("Docker version 25.0.3, build abc123"),
    )
    rc = install_deps.detect_and_instruct(greffer_id="abc-123")
    assert rc == 0
    out = capsys.readouterr().out
    assert "25.0.3" in out


def test_detect_and_instruct_docker_missing_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _fail())
    rc = install_deps.detect_and_instruct(greffer_id="abc-123")
    assert rc != 0
