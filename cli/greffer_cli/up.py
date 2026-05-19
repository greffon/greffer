"""``greffer up`` — the all-in-one operator command.

Walks the four-state state machine: Starting → Registering →
Awaiting cert → Connected. Idempotent: re-running on a connected
greffer fast-paths through (no rewrites, no restarts) and exits 0.

This file owns the state-machine driver. Config writing, doctor, and
install-deps are delegated to their modules.
"""

from __future__ import annotations

import datetime as dt
import enum
import time
from pathlib import Path
from typing import Literal

from . import compose, doctor, env_file, install_deps, manager_client, paths, strings


Mode = Literal["tunnel", "proxy"]


class StateMachineState(enum.Enum):
    STARTING = "Starting"
    REGISTERING = "Registering"
    AWAITING_CERT = "Awaiting cert"
    CONNECTED = "Connected"


# --- env.env writing -------------------------------------------------

def _build_env_values(
    *,
    manager_url: str,
    greffer_id: str,
    mode: Mode,
    address: str | None,
    public_host: str | None,
) -> dict[str, str]:
    """Build the env.env values to write for the chosen mode.

    See HLD § Current State for the eight env vars and what defaults
    each one has. Proxy mode requires --address and --public-host;
    tunnel mode does not.
    """
    values: dict[str, str] = {
        "GREFFER_ID": greffer_id,
        "GREFFON_BASE_SERVER": manager_url,
        "GREFFER_MODE": mode,
        # Service defaults that the rendered compose passes explicitly so
        # the source of truth is the env.env, not pydantic-settings:
        "GREFFER_PORT": "8001",
        "GREFFER_PROTOCOL": "https",
        "GREFFER_SSL_VERIFY": "true",
        "GREFFER_WORKERS_ENABLED": "true",
        "GREFFON_PATH": "/data",
    }
    if mode == "proxy":
        if not address or not public_host:
            raise ValueError(
                "proxy mode requires --address and --public-host"
            )
        values["GREFFER_ADDRESS"] = address
        values["GREFFER_PUBLIC_HOST"] = public_host
    return values


def write_config(
    config_dir: Path,
    template_text: str,
    image_tag: str,
    *,
    env_values: dict[str, str],
) -> None:
    """Render the compose template + write env.env. Atomic, 0600 perms."""
    config_dir.mkdir(parents=True, exist_ok=True)

    # Render compose template by string substitution on <TAG>.
    compose_text = template_text.replace("<TAG>", image_tag)
    compose_path = paths.docker_compose_yml_path(config_dir)
    compose_path.write_text(compose_text, encoding="utf-8")

    # Write env.env atomically + 0600.
    env = env_file.EnvFile(values=dict(env_values))
    env.write_atomic(paths.env_env_path(config_dir))


def already_initialized_for(config_dir: Path, greffer_id: str) -> bool:
    """Is the on-disk env.env for THIS greffer_id?"""
    env = env_file.EnvFile.read(paths.env_env_path(config_dir))
    return env.get("GREFFER_ID") == greffer_id


def existing_greffer_id(config_dir: Path) -> str | None:
    env = env_file.EnvFile.read(paths.env_env_path(config_dir))
    return env.get("GREFFER_ID")


# --- State machine ---------------------------------------------------

def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def profile_for_mode(mode: Mode) -> str | None:
    """Map operator-facing mode to the docker compose profile name."""
    return "tunnel" if mode == "tunnel" else None


def wait_for_compose_running(
    compose_file: Path,
    *,
    profile: str | None = None,
    timeout: float = 600.0,
    poll_interval: float = 2.0,
) -> bool:
    """Block until the relevant services show 'running'.

    Pass ``profile="tunnel"`` so the tunnel-sidecar is included in the
    "all running?" check. In proxy mode (no profile), only greffer +
    nginx are visible to ``compose ps``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        services = compose.compose_services_running(compose_file, profile=profile)
        if services and all(services.values()):
            return True
        time.sleep(poll_interval)
    return False


def wait_for_state(
    manager_url: str, greffer_id: str, target: str,
    *, timeout: float = 600.0,
    heartbeat_interval: float = 30.0,
    on_heartbeat=None,
) -> bool:
    """Poll the manager's state-public/ until ``state == target``.

    Returns True on match, False on timeout. Calls ``on_heartbeat()``
    every ``heartbeat_interval`` while still waiting — used by the
    state-machine driver to print "still waiting for admin" messages.
    """
    deadline = time.monotonic() + timeout
    last_heartbeat = time.monotonic()
    seen_unknown: set[str] = set()
    # Pass the deadline INTO poll_state — its transient-error retry loop
    # would otherwise spin forever without yielding, never letting the
    # for-body run the deadline check below. (Codex regression on a
    # prior fix that added ManagerUnreachable retries.)
    try:
        for state in manager_client.poll_state(
            manager_url, greffer_id, deadline=deadline,
        ):
            if state.state == target:
                return True
            # Forward-compat: log unrecognized states once and continue
            # polling. Lets future manager states coexist with older CLIs.
            if state.state not in (
                "GREFFER_CREATED", "GREFFER_REGISTERING", "GREFFER_REGISTERED",
            ) and state.state not in seen_unknown:
                seen_unknown.add(state.state)
                print(f"(unknown manager state '{state.state}' — continuing)")
            if time.monotonic() >= deadline:
                return False
            if (
                on_heartbeat
                and time.monotonic() - last_heartbeat >= heartbeat_interval
            ):
                on_heartbeat()
                last_heartbeat = time.monotonic()
    except manager_client.ManagerUnreachable:
        # poll_state hit the deadline during a sustained outage and
        # propagated. Surface as a normal "timed out" return so the
        # state-machine driver shows the right stuck-state hint.
        return False
    return False


def reachability_self_test(
    public_host: str, port: int, expected_id: str, *, timeout: float = 5.0,
) -> tuple[str, dict]:
    """Probe ``https://{public_host}:{port}/healthz`` and verify identity.

    Returns ``(outcome, context)`` where outcome is one of:
      - "ok" — 200 with matching ``id``
      - "wrong_id" — 200 but ``id`` mismatch
      - "transport_error" — DNS / TCP / TLS failure
      - "bad_status" — non-200, e.g. 502

    Proxy mode only — tunnel mode skips this step (Stem fronts end-user
    routing, the operator's host can't probe the Stem-served URL
    meaningfully).
    """
    import httpx
    url = f"https://{public_host}:{port}/healthz"
    try:
        r = httpx.get(url, verify=False, timeout=timeout)
    except httpx.HTTPError as exc:
        return ("transport_error", {"error": str(exc)})

    if r.status_code != 200:
        return ("bad_status", {"status": r.status_code})

    try:
        seen_id = r.json().get("id")
    except ValueError:
        return ("bad_status", {"status": r.status_code, "reason": "non-JSON body"})

    if seen_id != expected_id:
        return ("wrong_id", {"seen": seen_id, "expected": expected_id})

    return ("ok", {})
