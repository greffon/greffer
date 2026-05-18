"""Tests for greffer_cli.status — graceful degradation when Docker is broken."""

from __future__ import annotations

from pathlib import Path

import pytest

from greffer_cli import compose, status


def _fail(returncode: int, stderr: str) -> compose.CommandResult:
    return compose.CommandResult(returncode=returncode, stdout="", stderr=stderr)


def test_healthz_safe_daemon_down_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a stopped daemon used to render as "greffer /healthz:
    not responding" (an app problem), pointing operators at the wrong
    remediation. It now reports unavailable, the same as docker missing."""
    monkeypatch.setattr(
        compose, "exec_in_greffer_healthz",
        lambda _f: _fail(1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
    )
    ok, unavailable = status._healthz_safe(Path("/dev/null"))
    assert ok is False
    assert unavailable is True


def test_healthz_safe_no_such_container_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stack not started yet — exec couldn't even reach a container."""
    monkeypatch.setattr(
        compose, "exec_in_greffer_healthz",
        lambda _f: _fail(1, 'Error: No such service: "greffer"'),
    )
    ok, unavailable = status._healthz_safe(Path("/dev/null"))
    assert ok is False
    assert unavailable is True


def test_healthz_safe_docker_binary_missing_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        compose, "exec_in_greffer_healthz",
        lambda _f: _fail(127, "command not found: docker"),
    )
    ok, unavailable = status._healthz_safe(Path("/dev/null"))
    assert ok is False
    assert unavailable is True


def test_healthz_safe_app_failure_is_not_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-container probe that exits non-zero with no daemon/container
    error in stderr is a real app failure — we want operators routed to
    'check greffer logs', not 'check docker'."""
    monkeypatch.setattr(
        compose, "exec_in_greffer_healthz",
        lambda _f: _fail(1, "urllib.error.HTTPError: 503"),
    )
    ok, unavailable = status._healthz_safe(Path("/dev/null"))
    assert ok is False
    assert unavailable is False
