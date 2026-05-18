"""Unit tests for greffer_cli.doctor — each check fails independently."""

from __future__ import annotations

import pytest

from greffer_cli import compose, doctor, manager_client


# We mock the subprocess + httpx calls (compose.docker_version, etc.)
# rather than invoking real Docker — the doctor logic is what we're
# testing, not the underlying tools.


def _ok(stdout: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stderr: str = "") -> compose.CommandResult:
    return compose.CommandResult(returncode=returncode, stdout="", stderr=stderr)


def test_doctor_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "docker_version", lambda: _ok('{"Client":{"Version":"25.0.3"}}'))
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
    monkeypatch.setattr(compose, "docker_version", lambda: _fail(returncode=127))
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
    monkeypatch.setattr(compose, "docker_version", lambda: _ok('{"Client":{"Version":"25"}}'))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _fail())
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)
    monkeypatch.setattr(manager_client, "manager_reachable", lambda url, **_: True)

    results = doctor.run(manager_url="https://api.example.com")
    by_name = {r.name: r for r in results}
    assert by_name["docker_daemon"].passed is False
    assert by_name["docker_daemon"].skipped is False  # ran, failed
    assert by_name["port_free"].passed is True
    assert doctor.is_blocking_failure(results)


def test_doctor_port_taken_is_a_blocking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "docker_version", lambda: _ok('{"Client":{"Version":"25"}}'))
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
    monkeypatch.setattr(compose, "docker_version", lambda: _ok('{"Client":{"Version":"25"}}'))
    monkeypatch.setattr(compose, "docker_compose_version", lambda: _ok("v2"))
    monkeypatch.setattr(compose, "docker_info", lambda: _ok("server"))
    monkeypatch.setattr(compose, "host_port_free", lambda port: True)

    results = doctor.run(manager_url=None)
    by_name = {r.name: r for r in results}
    assert by_name["manager_url"].skipped is True
    # All other checks pass → not a blocking failure.
    assert not doctor.is_blocking_failure(results)


def test_doctor_format_report_lists_all_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "docker_version", lambda: _ok('{"Client":{"Version":"25"}}'))
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
