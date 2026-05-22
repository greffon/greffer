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
import sys
import time
from pathlib import Path
from typing import Callable, Literal

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
) -> dict[str, str]:
    """Build the env.env values to write for the chosen mode.

    Proxy mode requires --address (the manager-callback hostname/IP).
    Tunnel mode does not require it.

    GREFFER_PUBLIC_HOST is intentionally NOT written: the greffer's
    end-user URLs are constructed by the manager (wildcard subdomain,
    ``ports[].url``) and shipped to the greffer at start_greffon time;
    the greffer's compose-rendering code uses GREFFER_PUBLIC_HOST only
    as a dev/test fallback when the manager-supplied URL is missing.
    Asking the operator to type a value that production never uses was
    legacy redundancy.
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
        if not address:
            raise ValueError("proxy mode requires --address")
        values["GREFFER_ADDRESS"] = address
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


# --- Cert-installed waiter -------------------------------------------

def wait_for_cert_installed(
    compose_file: Path, *, timeout: float = 600.0, poll_interval: float = 2.0,
) -> bool:
    """Proxy-mode: poll the nginx sidecar until /root/pem.crt exists + non-empty.

    Returns True on success, False on timeout. Tunnel mode does NOT
    call this — the Stem-client sidecar's cert-install signal is TBD
    per the Stem HLD, so tunnel-mode Awaiting-cert is gated only on
    the manager state transition + the healthz probe (see
    ``run_state_machine``).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = compose.exec_nginx_cert_installed(compose_file)
        if result.ok:
            return True
        time.sleep(poll_interval)
    return False


def _wait_for_healthz(
    compose_file: Path, *, timeout: float = 30.0, poll_interval: float = 2.0,
) -> bool:
    """Brief wait for the in-container healthz probe to return 200.

    Used as the final "Connected" gate. The longer per-state timeouts
    already cover the slow paths; this is a short final-confirmation
    poll because the cert install can cause a momentary nginx reload
    even after the file appears.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = compose.exec_in_greffer_healthz(compose_file)
        if result.ok:
            return True
        time.sleep(poll_interval)
    return False


# --- The state-machine driver ----------------------------------------

# Outcome codes the driver returns. Anything non-zero means the
# operator should look at the printed hint and re-run after fixing.
EXIT_OK = 0
EXIT_TIMEOUT_STARTING = 10
EXIT_TIMEOUT_REGISTERING = 11
EXIT_TIMEOUT_AWAITING_CERT = 12
EXIT_TIMEOUT_HEALTHZ = 13
EXIT_GREFFER_NOT_FOUND = 14  # state-public/ 404 — wrong manager URL or unknown UUID
EXIT_COMPOSE_UP_FAILED = 20


def _build_reachability_line(
    outcome: str, ctx: dict, *, public_host: str, port: int,
) -> str:
    """Map the reachability_self_test outcome to the right string from strings.py."""
    if outcome == "ok":
        return strings.REACHABILITY_OK
    if outcome == "wrong_id":
        return strings.REACHABILITY_WRONG_ID.format(
            public_host=public_host, port=port,
            seen=ctx.get("seen", "?"), expected=ctx.get("expected", "?"),
        )
    if outcome == "transport_error":
        return strings.REACHABILITY_TRANSPORT_ERROR.format(
            public_host=public_host, port=port,
        )
    # bad_status or anything else
    return strings.REACHABILITY_BAD_STATUS


def run_state_machine(
    cfg: Path,
    *,
    manager_url: str,
    greffer_id: str,
    mode: Mode,
    address: str | None = None,
    port: int = 8001,
    timeout: float = 600.0,
    starter: Callable[..., compose.CommandResult] | None = None,
) -> int:
    """Walk Starting → Registering → Awaiting cert → Connected.

    Returns ``EXIT_OK`` on success, a non-zero code on timeout/failure.
    Prints a timestamped one-liner per state transition and a
    heartbeat every 30s while stuck in Registering.

    Per-state timeout is ``timeout`` seconds (10 min default). The
    total wall-clock for a full happy-path run is dominated by the
    Registering wait (admin acceptance latency); fast-pathing an
    already-Connected greffer is sub-second because all three checks
    pass on the first probe.

    ``starter`` is the injection point for the ``docker compose up``
    call — tests pass a mock; production passes ``None`` and we use
    ``compose.compose_up`` directly. Keeps the orchestration unit-testable
    without standing up Docker.
    """
    compose_file = paths.docker_compose_yml_path(cfg)
    profile = profile_for_mode(mode)
    starter = starter or compose.compose_up

    # ---- 1. Starting ----------------------------------------------------
    print(strings.STATE_STARTING.format(ts=_now()))

    # Fast-path: if every relevant service is already running, skip
    # `compose up` (it's idempotent but adds 1-2s and prints noise).
    # This is what makes a second `greffer up` invocation sub-second.
    services = compose.compose_services_running(compose_file, profile=profile)
    already_up = bool(services) and all(services.values())
    if not already_up:
        # ``compose.compose_up`` declares ``profile`` as keyword-only,
        # so we pass it as a kwarg here — a positional call would
        # raise TypeError on every cold start (Codex P1).
        up_result = starter(compose_file, profile=profile)
        if not up_result.ok:
            print(
                f"  ✗ docker compose up failed (exit {up_result.returncode}):\n"
                f"{up_result.stderr}",
                file=sys.stderr,
            )
            return EXIT_COMPOSE_UP_FAILED

    if not wait_for_compose_running(
        compose_file, profile=profile, timeout=timeout,
    ):
        print(strings.TIMEOUT_STARTING.format(
            minutes=int(timeout // 60),
            compose_path=compose_file,
        ), file=sys.stderr)
        return EXIT_TIMEOUT_STARTING

    # ---- 2. Registering -------------------------------------------------
    # We deliberately do NOT print a clickable accept URL. The --manager
    # value is the API URL (REACT_APP_API in manager-front), which is
    # often on a different host than the frontend (e.g. api.example.com
    # vs app.example.com) — synthesizing a UI URL from it is wrong in
    # the configs where it matters. Print the greffer ID instead and
    # let the admin find the matching card on the Greffers page.
    print(strings.STATE_REGISTERING.format(
        ts=_now(), greffer_id=greffer_id,
    ))

    def _heartbeat() -> None:
        print(strings.STATE_REGISTERING_HEARTBEAT.format(
            ts=_now(), greffer_id=greffer_id,
        ))

    # Manager state GREFFER_REGISTERED == admin has accepted. We don't
    # gate on seeing GREFFER_REGISTERING first — the greffer's
    # register-worker may transition through it in under a poll
    # interval, in which case we'd never see it. Land on REGISTERED.
    #
    # GrefferNotFound (state-public 404) gets a distinct exit code +
    # hint — the operator usually has the wrong --manager URL OR was
    # given a UUID that doesn't exist on this manager. Without this
    # catch, the wired-up `main.up` path would propagate the exception
    # to Typer and dump a traceback instead of a clean failure.
    try:
        reached = wait_for_state(
            manager_url, greffer_id, target="GREFFER_REGISTERED",
            timeout=timeout,
            heartbeat_interval=30.0,
            on_heartbeat=_heartbeat,
        )
    except manager_client.GrefferNotFound:
        print(
            f"  ✗ Manager at {manager_url} doesn't know greffer ID {greffer_id}.\n"
            f"    → check the --manager URL (typo? wrong environment?)\n"
            f"    → confirm the UUID with your admin (they create the row first\n"
            f"      via the manager admin UI; the install command then references it).",
            file=sys.stderr,
        )
        return EXIT_GREFFER_NOT_FOUND
    if not reached:
        print(strings.TIMEOUT_REGISTERING.format(
            minutes=int(timeout // 60),
            greffer_id=greffer_id,
            compose_path=compose_file,
        ), file=sys.stderr)
        return EXIT_TIMEOUT_REGISTERING

    # ---- 3. Awaiting cert -----------------------------------------------
    print(strings.STATE_AWAITING_CERT.format(ts=_now()))

    # Proxy mode: cert lands in the nginx sidecar at /root/pem.crt.
    # Tunnel mode: no equivalent local probe yet (Stem HLD TBD); the
    # state transition + healthz check below cover us.
    if mode == "proxy":
        if not wait_for_cert_installed(compose_file, timeout=timeout):
            print(strings.TIMEOUT_AWAITING_CERT.format(
                minutes=int(timeout // 60),
                compose_path=compose_file,
            ), file=sys.stderr)
            return EXIT_TIMEOUT_AWAITING_CERT

    # ---- 4. Connected ---------------------------------------------------
    # Final gate: healthz returns 200 from inside the greffer container.
    # Honor --timeout so slow hosts (or post-cert-install reload latency)
    # don't false-negative. The default 600s is plenty; operators with
    # a short --timeout get what they asked for.
    if not _wait_for_healthz(compose_file, timeout=timeout):
        print(
            "  ✗ container is running and cert is installed, but /healthz "
            "isn't responding. Try `docker compose -f "
            f"{compose_file} logs greffer`.",
            file=sys.stderr,
        )
        return EXIT_TIMEOUT_HEALTHZ

    # Success — print the mode-appropriate Connected message.
    # The reachability self-test that used to live here was diagnosing
    # --public-host misconfiguration. Since --public-host is gone (the
    # manager constructs end-user URLs, not the operator), the test's
    # main use case is too. The address-based connectivity check is
    # already implicit: if registration completed, the manager could
    # reach the greffer at --address.
    if mode == "tunnel":
        print(strings.CONNECTED_TUNNEL.format(
            greffer_id=greffer_id,
            manager_url=manager_url,
        ))
    else:
        print(strings.CONNECTED_PROXY.format(
            greffer_id=greffer_id,
            address=address or "?",
            manager_url=manager_url,
        ))

    return EXIT_OK
