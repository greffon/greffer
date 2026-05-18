"""Unit tests for greffer_cli.doctor — each check fails independently."""

from __future__ import annotations

import pytest

from greffer_cli import compose, doctor, manager_client


# We mock the subprocess + httpx calls (compose.docker_cli_installed, etc.)
# rather than invoking real Docker — the doctor logic is what we're
# testing, not the underlying tools.


def _ok(stdout: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stderr: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=returncode, stdout="", stderr=stderr)


def _cli_ok(version: str = "25.0.3") -> compose.CommandResult:
    """Mock of ``docker --version`` (daemon-independent installation check).

    The real command emits plain text — ``Docker version 25.0.3, build abc123`` —
    NOT JSON. Earlier mocks used JSON, which silently exercised the
    ``_extract_docker_version`` error-fallback path instead of the real
    parser. Fixed here so tests assert the actual production code path.
    """
    return _ok(f"Docker version {version}, build abc123")


def test_doctor_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _cli_ok())
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2.24.5"))
    monkeypatch.setattr(compose, "docker_info", lambda: _ok("server"))
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)
    monkeypatch.setattr(manager_client, "manager_reachable", lambda url, **_: True)

    results = doctor.run(manager_url="https://api.example.com")
    assert all(r.passed for r in results)
    assert not doctor.is_blocking_failure(results)
    report = doctor.format_report(results)
    assert "All checks passed" in report


def test_doctor_docker_missing_skips_dependent_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """When docker isn't installed, the compose-plugin and daemon checks skip."""
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _fail(returncode=127))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _ok("server"))
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)
    monkeypatch.setattr(manager_client, "manager_reachable", lambda url, **_: True)

    results = doctor.run(manager_url="https://api.example.com")
    by_name = {r.name: r for r in results}
    assert by_name["docker_installed"].passed is False
    assert by_name["compose_plugin"].skipped is True
    assert by_name["docker_daemon"].skipped is True
    # Port + manager checks are independent of docker.
    assert by_name["port_free"].passed is True
    assert by_name["manager_url"].passed is True
    assert doctor.is_blocking_failure(results)


def test_doctor_daemon_down_does_not_skip_port_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker installed but daemon down — port check still runs."""
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _cli_ok("25"))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _fail())
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)
    monkeypatch.setattr(manager_client, "manager_reachable", lambda url, **_: True)

    results = doctor.run(manager_url="https://api.example.com")
    by_name = {r.name: r for r in results}
    # The Codex finding: a stopped daemon must NOT be reported as
    # "Docker not installed" — those are independent checks with
    # different remediations ("start the daemon" vs "install Docker").
    assert by_name["docker_installed"].passed is True
    assert by_name["docker_installed"].skipped is False
    assert by_name["compose_plugin"].passed is True  # daemon-independent too
    assert by_name["docker_daemon"].passed is False
    assert by_name["docker_daemon"].skipped is False  # ran, failed
    assert by_name["port_free"].passed is True
    assert doctor.is_blocking_failure(results)


def test_doctor_port_taken_is_a_blocking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _cli_ok("25"))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _ok("server"))
    monkeypatch.setattr(compose, "host_port_free", lambda port: False)
    monkeypatch.setattr(manager_client, "manager_reachable", lambda url, **_: True)

    results = doctor.run(manager_url="https://api.example.com")
    by_name = {r.name: r for r in results}
    assert by_name["port_free"].passed is False
    assert doctor.is_blocking_failure(results)


def test_doctor_no_manager_url_skips_manager_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-init invocation: no env.env exists → manager URL unknown →
    the manager-reachability check skips with an informational note
    rather than failing."""
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _cli_ok("25"))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _ok("server"))
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)

    results = doctor.run(manager_url=None)
    by_name = {r.name: r for r in results}
    assert by_name["manager_url"].skipped is True
    # All other checks pass → not a blocking failure.
    assert not doctor.is_blocking_failure(results)


def test_doctor_format_report_lists_all_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "docker_cli_installed", lambda: _cli_ok("25"))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _ok("server"))
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)
    monkeypatch.setattr(manager_client, "manager_reachable", lambda url, **_: True)

    results = doctor.run(manager_url="https://api.example.com")
    report = doctor.format_report(results)
    assert "Docker installed" in report
    assert "Compose plugin" in report
    assert "Docker daemon" in report
    assert "Host port" in report
    assert "Manager URL" in report
    # The version actually surfaces in the pass line — regression guard
    # against the Codex finding that `_extract_docker_version` was being
    # fed plain text but parsing JSON, silently dropping the version.
    assert "25" in report
