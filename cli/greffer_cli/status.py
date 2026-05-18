"""``greffer status`` — read-only state report.

Surfaces four signals: greffer container state, manager-side
registration state, local /healthz result, and (proxy-mode only)
cert-installed-in-nginx check. Graceful degradation: every check
that can fail prints a "manager unreachable" / "compose unavailable"
line and the command continues to completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import compose, env_file, manager_client, paths, up


@dataclass
class StatusReport:
    initialized: bool
    config_dir: Path
    greffer_id: str | None
    manager_url: str | None
    mode: str | None
    address: str | None
    public_host: str | None
    container_states: dict[str, bool]
    manager_state: str | None
    manager_unreachable: bool
    healthz_ok: bool
    healthz_unreachable: bool
    cert_installed: bool | None  # None if mode != proxy
    cert_check_unavailable: bool


def collect(config_dir: Path) -> StatusReport:
    env_path = paths.env_env_path(config_dir)
    if not env_path.exists():
        return StatusReport(
            initialized=False,
            config_dir=config_dir,
            greffer_id=None,
            manager_url=None,
            mode=None,
            address=None,
            public_host=None,
            container_states={},
            manager_state=None,
            manager_unreachable=False,
            healthz_ok=False,
            healthz_unreachable=False,
            cert_installed=None,
            cert_check_unavailable=False,
        )

    env = env_file.EnvFile.read(env_path)
    greffer_id = env.get("GREFFER_ID")
    manager_url = env.get("GREFFON_BASE_SERVER")
    mode = env.get("GREFFER_MODE") or "tunnel"
    address = env.get("GREFFER_ADDRESS")
    public_host = env.get("GREFFER_PUBLIC_HOST")

    compose_file = paths.docker_compose_yml_path(config_dir)
    # Surface the tunnel-sidecar in tunnel mode (it's profile-gated).
    # Delegate the mode→profile mapping to up.profile_for_mode so the
    # two call sites can't drift if a third mode is ever added.
    profile = up.profile_for_mode(mode)  # type: ignore[arg-type]
    container_states = compose.compose_services_running(compose_file, profile=profile)

    manager_state, manager_unreachable = _manager_state_safe(manager_url, greffer_id)
    healthz_ok, healthz_unreachable = _healthz_safe(compose_file)
    cert_installed, cert_check_unavailable = _cert_installed_safe(compose_file, mode)

    return StatusReport(
        initialized=True,
        config_dir=config_dir,
        greffer_id=greffer_id,
        manager_url=manager_url,
        mode=mode,
        address=address,
        public_host=public_host,
        container_states=container_states,
        manager_state=manager_state,
        manager_unreachable=manager_unreachable,
        healthz_ok=healthz_ok,
        healthz_unreachable=healthz_unreachable,
        cert_installed=cert_installed,
        cert_check_unavailable=cert_check_unavailable,
    )


def _manager_state_safe(
    manager_url: str | None, greffer_id: str | None,
) -> tuple[str | None, bool]:
    if not manager_url or not greffer_id:
        return None, True
    try:
        state = manager_client.fetch_state(manager_url, greffer_id, timeout=5.0)
    except manager_client.GrefferNotFound:
        return "NOT_FOUND", False
    except manager_client.ManagerUnreachable:
        return None, True
    except Exception:
        return None, True
    return state.state, False


def _healthz_safe(compose_file: Path) -> tuple[bool, bool]:
    """In-container healthz probe; degrades gracefully if Docker is missing.

    Returns ``(ok, unavailable)``. ``unavailable`` covers ALL the
    "couldn't run the probe" cases — not just exit 127 (binary missing)
    but also a stopped daemon and a stopped/absent container. Without
    this distinction, a daemon-down host reports "/healthz: not
    responding" (an app problem), sending operators to the wrong
    remediation. Stderr matching is heuristic but matches Docker's
    error strings as of the 20.10+ CLI.
    """
    result = compose.exec_in_greffer_healthz(compose_file)
    if result.returncode == 127:  # docker binary not on PATH
        return False, True
    if not result.ok:
        stderr = (result.stderr or "").lower()
        # Daemon down / DOCKER_HOST broken / not in docker group:
        if "cannot connect to the docker daemon" in stderr:
            return False, True
        # Container absent (stack not started, or wrong compose file):
        if (
            "no such container" in stderr
            or "no such service" in stderr
            or "is not running" in stderr
        ):
            return False, True
    return result.ok, False


def _cert_installed_safe(
    compose_file: Path, mode: str,
) -> tuple[bool | None, bool]:
    """Proxy-mode cert check. Tunnel mode returns (None, True) — TBD per Stem HLD."""
    if mode != "proxy":
        return None, True
    result = compose.exec_nginx_cert_installed(compose_file)
    if result.returncode == 127:
        return None, True
    return result.ok, False


def format_report(report: StatusReport) -> str:
    if not report.initialized:
        return (
            "not initialized — run `greffer up`\n"
            f"(checked {paths.env_env_path(report.config_dir)})"
        )

    lines = [
        "greffer status",
        "",
        f"  Greffer ID:  {report.greffer_id}",
        f"  Manager:     {report.manager_url}",
        f"  Mode:        {report.mode}",
    ]
    if report.mode == "proxy":
        lines.append(f"  Address:     {report.address}")
        lines.append(f"  Public host: {report.public_host}")
    lines.append("")

    if report.container_states:
        for name, running in sorted(report.container_states.items()):
            mark = "✓" if running else "✗"
            state_str = "running" if running else "not running"
            lines.append(f"  {mark} container {name}: {state_str}")
    else:
        lines.append("  ⊘ container state: no container found (run `greffer up`)")

    if report.manager_unreachable:
        lines.append("  ⊘ manager state: manager unreachable")
    elif report.manager_state == "NOT_FOUND":
        lines.append("  ✗ manager state: this greffer ID is not known to the manager")
    else:
        lines.append(f"  ✓ manager state: {report.manager_state}")

    if report.healthz_unreachable:
        lines.append("  ⊘ greffer /healthz: container not reachable")
    elif report.healthz_ok:
        lines.append("  ✓ greffer /healthz: 200")
    else:
        lines.append("  ✗ greffer /healthz: not responding")

    if report.cert_installed is True:
        lines.append("  ✓ cert installed in nginx sidecar")
    elif report.cert_installed is False:
        lines.append("  ✗ cert NOT installed in nginx sidecar")
    elif report.mode == "proxy":
        # Proxy mode + cert_installed is None → the check itself couldn't
        # run (e.g. Docker missing). Surface that explicitly; silently
        # dropping the line would leave operators unsure whether the
        # check was skipped or forgotten.
        lines.append("  ⊘ cert check: unavailable (docker not reachable)")
    else:
        lines.append(f"  ⊘ cert check: skipped in {report.mode} mode (TBD per Stem HLD)")

    return "\n".join(lines)
