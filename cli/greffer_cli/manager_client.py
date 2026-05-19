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

    # Normalize any other non-2xx into the typed exceptions the caller
    # already handles. Without this, a transient 502/503/504 from the
    # manager would leak through poll_state (which only catches
    # _RateLimited) as a raw httpx.HTTPStatusError and abort polling
    # mid-flight. Map them all to ManagerUnreachable so the caller
    # gets retry semantics consistent with a transport-level failure.
    if r.status_code >= 400:
        raise ManagerUnreachable(f"manager returned HTTP {r.status_code}")

    try:
        body = r.json()
    except ValueError as exc:
        raise ManagerUnreachable(f"manager returned non-JSON body: {exc}") from exc
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
    deadline: float | None = None,
) -> Iterator[StatePublic]:
    """Generator that yields successive ``StatePublic`` snapshots.

    Implements the design's back-off policy: poll every 2s by default;
    on 429 back off to 5s, then 30s on a subsequent 429 (honoring
    ``Retry-After`` when the manager sends it). Raises ``GrefferNotFound``
    or ``ManagerUnreachable`` to the caller — the caller decides what
    a stuck greffer means.

    ``deadline`` (optional, ``time.monotonic()`` seconds) bounds the
    transient-retry loop: if set, a sustained ``ManagerUnreachable``
    re-raises once the deadline is past instead of looping forever.
    Without it, a prolonged manager outage would hang the caller
    silently — ``wait_for_state``'s outer timeout can't fire while
    we're inside this generator if we never yield.
    """
    interval = initial_interval
    while True:
        try:
            yield fetch_state(manager_url, greffer_id, timeout=timeout)
            interval = initial_interval  # back to fast cadence on success
        except _RateLimited as rl:
            # Also bound on deadline here: a manager that hammers us with
            # 429 (or a long Retry-After) would otherwise let us sleep
            # well past the caller's --timeout. Re-raise as
            # ManagerUnreachable so the caller's handler treats it the
            # same as a sustained outage and returns False on timeout.
            if deadline is not None and time.monotonic() >= deadline:
                raise ManagerUnreachable(
                    f"rate-limited past deadline; last retry-after={rl.retry_after}s"
                ) from rl
            sleep_for = max(rl.retry_after, interval)
            # Cap sleep to remaining budget: a 3600s Retry-After must
            # not extend a --timeout=600 invocation to an hour.
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)
            interval = min(
                interval * 2 if interval >= rate_limited_interval else rate_limited_interval,
                rate_limited_interval_max,
            )
            continue
        except ManagerUnreachable:
            # Transient transport / 5xx — back off on the same schedule
            # as a rate-limit and keep polling. The caller's own
            # deadline (e.g. `wait_for_state`'s timeout) decides when
            # to give up. GrefferNotFound (a terminal verdict) still
            # propagates because it's NOT a ManagerUnreachable.
            if deadline is not None and time.monotonic() >= deadline:
                raise
            sleep_for = interval
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)
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
