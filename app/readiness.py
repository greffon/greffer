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
# after success) and ``greffer-reregister`` is an idle supervisor (its death
# only delays a 403-driven re-register, which is recoverable), so neither is
# fatal here.
FATAL_WORKERS = ("greffer-monitor", "greffer-crl-sync", "greffer-heartbeat")


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
    """Ping the docker daemon. Imported lazily so unit tests run without the
    docker SDK initializing its ``from_env()`` client at import (mirrors
    status_collect / the monitor)."""
    try:
        from apps.utils.docker.base import client

        client.ping()
        return True
    except Exception:
        # A readiness probe must treat ANY ping failure as "daemon unreachable"
        # (the docker SDK raises APIError / connection / socket errors of
        # several types); logging keeps it from being a silent swallow.
        logger.warning("readiness: docker ping failed", exc_info=True)
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
