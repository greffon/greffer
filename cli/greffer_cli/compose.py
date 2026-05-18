"""Wrapper around ``docker`` and ``docker compose`` subprocess calls.

Every exec call uses ``-T`` to disable TTY allocation — without it,
``docker compose exec`` defaults to interactive TTY and fails in
non-interactive subprocess contexts (the CLI is one, CI is another)
with "the input device is not a TTY".
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run(args: list[str], *, timeout: float | None = None) -> CommandResult:
    """Run a subprocess; never raise on non-zero exit. Caller inspects."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        # docker isn't installed; surface as a non-zero result the
        # caller can interpret as "docker missing."
        return CommandResult(returncode=127, stdout="", stderr=f"command not found: {args[0]}")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=124, stdout=exc.stdout or "", stderr=f"timeout after {timeout}s",
        )
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


# --- Docker daemon / installation -----------------------------------

def docker_cli_installed() -> CommandResult:
    """``docker --version`` — daemon-independent. Tests "Docker CLI binary
    is on PATH"; does NOT contact the daemon. The dedicated daemon check
    uses ``docker info``.

    Note: ``docker version`` (no double-dash) contacts the daemon and
    fails when the daemon is down — we deliberately use the static
    ``docker --version`` here so a "daemon down" condition reports as
    "daemon not reachable" not "Docker not installed."
    """
    return _run(["docker", "--version"], timeout=10)


def docker_version() -> CommandResult:
    """``docker version --format json`` — full client + server info.

    Hits the daemon; will fail if the daemon is down. Use
    ``docker_cli_installed()`` for daemon-independent installation
    detection.
    """
    return _run(["docker", "version", "--format", "json"], timeout=10)


def docker_info() -> CommandResult:
    """``docker info`` — used by doctor to verify the daemon is reachable."""
    return _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)


def docker_compose_version() -> CommandResult:
    """``docker compose version --short`` — daemon-independent.

    The compose plugin is invoked as a docker subcommand but doesn't
    contact the daemon for ``version --short``; this works on a host
    where the daemon is down.
    """
    return _run(["docker", "compose", "version", "--short"], timeout=10)


# --- Compose lifecycle ----------------------------------------------

def compose_up(compose_file: Path, *, profile: str | None = None) -> CommandResult:
    """Bring the compose stack up. In tunnel mode, pass profile="tunnel"
    so the tunnel-sidecar service starts; in proxy mode (default) only
    the greffer + nginx services start.

    Profiles are how the single bundled compose.yml supports both modes:
    services with ``profiles: ["tunnel"]`` are skipped unless that
    profile is enabled.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    if profile:
        args.extend(["--profile", profile])
    args.extend(["up", "-d"])
    return _run(args, timeout=300)  # image pull can be slow


def compose_ps(compose_file: Path, *, profile: str | None = None) -> CommandResult:
    """List compose services, including stopped ones.

    Mode-aware via profile: without the profile, ``ps`` only lists
    default services even if a profiled service is also running. We
    pass the profile so tunnel-mode operators see all containers
    (greffer + nginx + tunnel-sidecar).

    We also pass ``--all``: by default ``docker compose ps`` only shows
    RUNNING services (per Docker's CLI docs), so a crashed/exited
    service would disappear entirely — making ``wait_for_compose_running``
    falsely report success because the failed service isn't in the
    dict to check. With ``--all`` it shows up with state != "running",
    which the caller correctly flags as not-yet-up.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    if profile:
        args.extend(["--profile", profile])
    args.extend(["ps", "--all", "--format", "json"])
    return _run(args, timeout=15)


def compose_services_running(
    compose_file: Path, *, profile: str | None = None,
) -> dict[str, bool]:
    """Parse ``compose ps --format json`` and return a dict of service → running.

    Compose v2 emits one JSON object per line (NDJSON-style). v1 emitted
    a single JSON array. We accept either. Pass ``profile`` to surface
    profiled services (e.g., the tunnel-sidecar in tunnel mode).
    """
    result = compose_ps(compose_file, profile=profile)
    if not result.ok:
        return {}
    text = result.stdout.strip()
    if not text:
        return {}
    # Be defensive: a compose-plugin warning printed to stdout (rare but
    # observed in the wild) would crash status with a JSONDecodeError.
    # Skip unparseable lines instead — better to under-report a service
    # than to abort the whole status command.
    services: dict[str, bool] = {}
    try:
        if text.startswith("["):
            items = json.loads(text)
        else:
            items = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except json.JSONDecodeError:
        return {}
    for item in items:
        name = item.get("Service") or item.get("Name")
        state = item.get("State") or item.get("status", "")
        if name:
            services[name] = state.lower() == "running"
    return services


# --- Local exec into the running greffer / nginx --------------------

def exec_in_greffer_healthz(compose_file: Path) -> CommandResult:
    """Probe the FastAPI app's ``/healthz`` from inside the greffer container.

    We hit the app's internal port (8000) from inside the container
    rather than the host's exposed nginx port — the host probe is the
    reachability self-test (proxy mode only) and depends on operator
    DNS / public-host setup; the in-container probe just verifies the
    app is up.

    We use ``python -c urllib.request`` rather than ``curl``: the
    greffer image is ``python:3.11-alpine`` and does NOT install curl
    (see greffer/Dockerfile). Python is guaranteed present — it's
    what runs uvicorn. Exit 0 iff the response status is 200.
    """
    # Catch HTTPError / URLError explicitly: ``urlopen`` raises
    # ``HTTPError`` for 4xx/5xx (so a 503 would never reach ``r.status``)
    # and ``URLError`` for connection refused. Without the try/except,
    # those manifest as a traceback piped through ``docker exec`` —
    # functionally non-zero exit, but noisy in logs. Clean exit 1 in
    # both error cases.
    probe = (
        "import sys, urllib.request, urllib.error;"
        "\ntry:"
        "\n    r = urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)"
        "\n    sys.exit(0 if r.status == 200 else 1)"
        "\nexcept (urllib.error.HTTPError, urllib.error.URLError):"
        "\n    sys.exit(1)"
    )
    return _run(
        [
            "docker", "compose", "-f", str(compose_file),
            "exec", "-T", "greffer",
            "python", "-c", probe,
        ],
        timeout=10,
    )


def exec_nginx_cert_installed(compose_file: Path) -> CommandResult:
    """Check that the cert was installed into the nginx sidecar.

    ``test -s`` verifies the file exists AND is non-empty (guards
    against placeholder / pre-write states). Only meaningful in proxy
    mode — tunnel mode uses a Stem-client sidecar instead, with a
    cert-installed signal that's TBD per the Stem HLD.
    """
    return _run(
        [
            "docker", "compose", "-f", str(compose_file),
            "exec", "-T", "nginx",
            "test", "-s", "/root/pem.crt",
        ],
        timeout=10,
    )


def docker_inspect_restart_count(container_id: str) -> int:
    """Return the container's RestartCount, or 0 if unavailable."""
    result = _run(
        ["docker", "inspect", container_id, "--format", "{{.RestartCount}}"],
        timeout=10,
    )
    if not result.ok:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def host_port_free(port: int) -> bool:
    """Doctor: is the given host port available for nginx to bind?"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def quote(args: list[str]) -> str:
    """Build a shell-readable command string for logging."""
    return " ".join(shlex.quote(a) for a in args)
