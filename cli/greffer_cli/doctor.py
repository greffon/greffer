"""``greffer doctor`` — read-only preflight checks.

Each check fails independently with a distinct hint. Checks that
depend on prior ones (e.g. "host port free" needs Docker to know
which port to ask about — though the port is hardcoded to 8001 in
practice) gracefully skip with a `⊘` glyph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import compose, manager_client, strings


@dataclass
class CheckResult:
    name: str
    passed: bool
    line: str  # The exact line to print (already formatted)
    skipped: bool = False


def run(manager_url: str | None, *, port: int = 8001) -> list[CheckResult]:
    """Execute all five doctor checks and return their results."""
    results: list[CheckResult] = []

    # 1. Docker installed
    docker_v = compose.docker_version()
    if not docker_v.ok:
        results.append(CheckResult(
            name="docker_installed", passed=False,
            line=strings.DOCTOR_FAIL_DOCKER,
        ))
        # Subsequent docker-dependent checks skip.
        results.append(CheckResult(
            name="compose_plugin", passed=False, skipped=True,
            line=strings.DOCTOR_SKIP.format(
                what="Compose plugin available", reason="Docker not installed",
            ),
        ))
        results.append(CheckResult(
            name="docker_daemon", passed=False, skipped=True,
            line=strings.DOCTOR_SKIP.format(
                what="Docker daemon reachable", reason="Docker not installed",
            ),
        ))
    else:
        # Parse version (best-effort — `docker version --format json` is
        # verbose; just show "Docker Engine" if we can't pick a clean line).
        version_line = _extract_docker_version(docker_v.stdout)
        results.append(CheckResult(
            name="docker_installed", passed=True,
            line=strings.DOCTOR_PASS_DOCKER.format(version=version_line),
        ))

        # 2. Compose plugin available
        compose_v = compose.docker_compose_version()
        if compose_v.ok:
            results.append(CheckResult(
                name="compose_plugin", passed=True,
                line=strings.DOCTOR_PASS_COMPOSE.format(version=compose_v.stdout.strip() or "v2"),
            ))
        else:
            results.append(CheckResult(
                name="compose_plugin", passed=False,
                line=strings.DOCTOR_FAIL_COMPOSE,
            ))

        # 3. Docker daemon reachable
        info = compose.docker_info()
        if info.ok:
            results.append(CheckResult(
                name="docker_daemon", passed=True,
                line=strings.DOCTOR_PASS_DAEMON,
            ))
        else:
            results.append(CheckResult(
                name="docker_daemon", passed=False,
                line=strings.DOCTOR_FAIL_DAEMON,
            ))

    # 4. Host port free
    if compose.host_port_free(port):
        results.append(CheckResult(
            name="port_free", passed=True,
            line=strings.DOCTOR_PASS_PORT.format(port=port),
        ))
    else:
        results.append(CheckResult(
            name="port_free", passed=False,
            line=strings.DOCTOR_FAIL_PORT.format(port=port),
        ))

    # 5. Manager URL reachable (skipped if no URL — pre-init doctor invocation)
    if manager_url:
        if manager_client.manager_reachable(manager_url):
            results.append(CheckResult(
                name="manager_url", passed=True,
                line=strings.DOCTOR_PASS_MANAGER.format(url=manager_url),
            ))
        else:
            results.append(CheckResult(
                name="manager_url", passed=False,
                line=strings.DOCTOR_FAIL_MANAGER.format(url=manager_url),
            ))
    else:
        results.append(CheckResult(
            name="manager_url", passed=False, skipped=True,
            line=strings.DOCTOR_SKIP.format(
                what="Manager URL reachable",
                reason="no env.env yet — invoke after `greffer up` writes config",
            ),
        ))

    return results


def _extract_docker_version(json_stdout: str) -> str:
    """Pull a short version string out of ``docker version --format json``.

    Best-effort — if parsing fails we fall back to "Docker Engine" so
    the doctor output stays clean.
    """
    import json
    try:
        data = json.loads(json_stdout)
        client = data.get("Client", {})
        version = client.get("Version") or "?"
        return f"Docker Engine {version}"
    except (json.JSONDecodeError, AttributeError, TypeError):
        return "Docker Engine"


def is_blocking_failure(results: list[CheckResult]) -> bool:
    """At least one non-skipped check failed → doctor blocks `up`."""
    return any(not r.passed and not r.skipped for r in results)


def format_report(results: list[CheckResult]) -> str:
    """Format the full doctor report for terminal output."""
    lines = [strings.DOCTOR_HEADER, ""]
    for r in results:
        lines.append(r.line)
    if is_blocking_failure(results):
        n_failed = sum(1 for r in results if not r.passed and not r.skipped)
        lines.append(strings.DOCTOR_FAILED_SUMMARY.format(n_failed=n_failed))
    else:
        lines.append(strings.DOCTOR_ALL_PASSED)
    return "\n".join(lines)
