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


def test_cert_installed_safe_daemon_down_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a stopped daemon in proxy mode used to render as "cert
    NOT installed," sending operators to cert debugging when the real
    issue is Docker. Now reports unavailable, same shape as healthz."""
    monkeypatch.setattr(
        compose, "exec_nginx_cert_installed",
        lambda _f: _fail(1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
    )
    installed, unavailable = status._cert_installed_safe(Path("/dev/null"), mode="proxy")
    assert installed is None
    assert unavailable is True


def test_cert_installed_safe_real_missing_cert_is_not_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Docker is fine but the cert file is genuinely missing (test -s
    exits 1, no daemon/container signal in stderr), we report
    ``cert NOT installed`` — operators DO need cert debugging here."""
    monkeypatch.setattr(
        compose, "exec_nginx_cert_installed",
        lambda _f: _fail(1, ""),
    )
    installed, unavailable = status._cert_installed_safe(Path("/dev/null"), mode="proxy")
    assert installed is False
    assert unavailable is False


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
