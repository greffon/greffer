"""Thin HTTP wrapper around the manager's ``state-public/`` endpoint.

The CLI talks to the manager from a not-yet-registered host, so it
only consumes endpoints that don't require auth: just ``state-public/``
(read-only) and a HEAD/GET probe of the manager root for the doctor
reachability check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

import httpx


@dataclass
class StatePublic:
    state: str  # GREFFER_CREATED, GREFFER_REGISTERING, GREFFER_REGISTERED, or UNKNOWN


class ManagerUnreachable(Exception):
    """Transport-layer failure talking to the manager (DNS / TCP / TLS / timeout)."""


class GrefferNotFound(Exception):
    """The greffer UUID doesn't exist on this manager (404 from state-public)."""


def fetch_state(manager_url: str, greffer_id: str, *, timeout: float = 5.0) -> StatePublic:
    """Single read of ``GET {manager}/api/greffer/<id>/state-public/``.

    Returns a ``StatePublic`` on 200. Raises ``GrefferNotFound`` on 404
    (unknown UUID on this manager) and ``ManagerUnreachable`` on any
    transport-layer error. The 429 (rate-limited) path is the caller's
    responsibility to handle — we surface the response without retrying
    so the caller can apply its own back-off policy.
    """
    url = f"{manager_url.rstrip('/')}/api/greffer/{greffer_id}/state-public/"
    try:
        r = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ManagerUnreachable(str(exc)) from exc

    if r.status_code == 404:
        raise GrefferNotFound(greffer_id)
    if r.status_code == 429:
        # Surface the rate-limit response to the caller; the heartbeat
        # back-off loop handles re-spacing.
        raise _RateLimited(retry_after=_retry_after_seconds(r))

    r.raise_for_status()
    body = r.json()
    state = body.get("state", "UNKNOWN")
    return StatePublic(state=state)


class _RateLimited(Exception):
    """Internal — the manager returned 429. Carry ``Retry-After`` if present."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate-limited; retry after {retry_after}s")


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse the ``Retry-After`` header (in seconds) or fall back to 5."""
    value = response.headers.get("retry-after")
    if not value:
        return 5.0
    try:
        return float(value)
    except ValueError:
        return 5.0


def poll_state(
    manager_url: str,
    greffer_id: str,
    *,
    initial_interval: float = 2.0,
    rate_limited_interval: float = 5.0,
    rate_limited_interval_max: float = 30.0,
    timeout: float = 5.0,
) -> Iterator[StatePublic]:
    """Generator that yields successive ``StatePublic`` snapshots.

    Implements the design's back-off policy: poll every 2s by default;
    on 429 back off to 5s, then 30s on a subsequent 429 (honoring
    ``Retry-After`` when the manager sends it). Raises ``GrefferNotFound``
    or ``ManagerUnreachable`` to the caller — the caller decides what
    a stuck greffer means.
    """
    interval = initial_interval
    while True:
        try:
            yield fetch_state(manager_url, greffer_id, timeout=timeout)
            interval = initial_interval  # back to fast cadence on success
        except _RateLimited as rl:
            time.sleep(max(rl.retry_after, interval))
            interval = min(
                interval * 2 if interval >= rate_limited_interval else rate_limited_interval,
                rate_limited_interval_max,
            )
            continue
        time.sleep(interval)


def manager_reachable(manager_url: str, *, timeout: float = 5.0) -> bool:
    """Doctor's manager-URL reachability gate.

    Any HTTP response from the manager URL counts as reachable — a
    Django/DRF manager without an explicit root view returns 404 or 405
    to HEAD on /, and that's healthy behavior. Gating on a specific
    code would false-positive on perfectly-configured managers.

    Returns False only on transport-layer errors (DNS, TCP refused,
    TLS handshake failure, connect timeout).
    """
    try:
        httpx.head(manager_url, timeout=timeout)
    except httpx.HTTPError:
        return False
    return True
