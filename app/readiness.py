"""Greffer self-readiness evaluation (greffer-observability epic, Feature #3).

Shared by the ``/readyz`` endpoint (operator / manager visibility) and the
in-process watchdog (self-heal). The fatal-vs-degraded split is deliberate:

- **fatal**: the greffer is wedged and a process restart is the right fix. The
  watchdog acts on a SUSTAINED fatal condition; the compose healthcheck surfaces
  it for ``docker compose ps``.
- **degraded**: something is off but a restart would not help (and could
  restart-loop). A greffer pending acceptance is the canonical case: it is
  healthy and MUST stay up until an admin accepts it.

Reasons are machine-readable tokens carried by both the endpoint body and the
watchdog's logs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import FastAPI

logger = logging.getLogger("greffer")

# Long-lived worker tasks whose death means the greffer is wedged (the loop is
# gone until a restart). ``greffer-register`` is one-shot (it exits normally
# after success), so it is excluded. ``greffer-reregister`` IS included: it is an
# idle supervisor, but it is the ONLY consumer of ``reregister_requested``, so if
# it dies a later heartbeat 403 parks heartbeats forever (the heartbeat clears
# ``registered`` and sets an event nobody reads) and the greffer cannot recover
# without a restart (codex P2 on greffon/greffer#72).
FATAL_WORKERS = (
    "greffer-monitor",
    "greffer-crl-sync",
    "greffer-heartbeat",
    "greffer-reregister",
)

# A short-timeout client dedicated to the readiness ping. The shared
# apps.utils.docker client uses the SDK default (~60s), so a HUNG (not down)
# daemon would leave the ping blocked. anyio's abandon_on_cancel stops WAITING
# on that thread but does not stop the ping, and the AnyIO worker threads are
# non-daemon, so a stuck ping keeps the process alive past the watchdog's
# graceful SIGTERM and `restart: unless-stopped` never sees a clean exit (codex
# P2 on greffon/greffer#72). A bounded client makes the ping itself fail fast.
_DOCKER_PING_TIMEOUT_SECONDS = 4
_ping_client = None


@dataclass
class Readiness:
    fatal: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.fatal:
            return "fatal"
        return "degraded" if self.reasons else "ready"


def _docker_ok() -> bool:
    """Ping the docker daemon with a short-timeout client so a hung daemon fails
    fast (see ``_DOCKER_PING_TIMEOUT_SECONDS`` above). Imported lazily so unit
    tests run without the docker SDK initializing a client at import (mirrors
    status_collect / the monitor)."""
    global _ping_client
    try:
        import docker

        if _ping_client is None:
            _ping_client = docker.from_env(
                timeout=_DOCKER_PING_TIMEOUT_SECONDS)
        _ping_client.ping()
        return True
    except Exception:
        # A readiness probe must treat ANY ping failure as "daemon unreachable"
        # (the docker SDK raises APIError / connection / socket errors of
        # several types); logging keeps it from being a silent swallow. Drop a
        # possibly-broken client so the next probe rebuilds it.
        logger.warning("readiness: docker ping failed", exc_info=True)
        _ping_client = None
        return False


def evaluate_readiness(app: FastAPI) -> Readiness:
    """Compute the greffer's readiness. Pure-ish (only reads ``app.state`` and
    pings docker), so the endpoint and the watchdog share one definition."""
    r = Readiness()

    # fatal: docker daemon unreachable. Every compose op depends on it; a
    # restart re-establishes the client.
    if not _docker_ok():
        r.fatal = True
        r.reasons.append("docker_unreachable")

    # fatal: a long-lived worker crashed. ``task.done()`` on a forever-loop
    # worker means it returned or raised; only a restart brings the loop back.
    tasks = getattr(app.state, "worker_tasks", None) or {}
    for name in FATAL_WORKERS:
        task = tasks.get(name)
        if task is not None and task.done():
            r.fatal = True
            r.reasons.append(f"worker_dead:{name}")

    # degraded: registration not yet accepted. The greffer is healthy and must
    # NOT be restarted while it waits for an admin to accept it.
    registered = getattr(app.state, "registered", None)
    if registered is not None and not registered.is_set():
        r.reasons.append("registration_pending")

    return r
