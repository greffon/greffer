"""Heartbeat worker — push greffer liveness to the manager every interval.

Greffer-observability epic, Feature #1. Each beat POSTs the full instance-status
map plus version/uptime/disk/seq to ``/api/greffer/<id>/heartbeat/``, so a dead
or unreachable greffer is distinguishable from a healthy idle one. The beat is
sent unconditionally: if status collection fails it goes out ``degraded`` with an
empty map, so the manager sees "alive but degraded" rather than "gone". A 403
asks the register supervisor to re-run registration (the manager rejected our
token). See docs/features/greffer-observability/hld-heartbeat.md.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone

import anyio
import requests
from fastapi import FastAPI

from app.diagnostics import diag
from app.settings import Settings
from app.workers.status_collect import collect_status_map

logger = logging.getLogger("greffer")

_HTTP_TIMEOUT_SECONDS = 10.0


async def heartbeat_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    seq = 0
    try:
        while True:
            # Gate every beat on registration being complete, so the heartbeat
            # never POSTs before the greffer is accepted (which would 403 every
            # beat during the admin-acceptance wait and trigger a concurrent
            # re-register). Cleared on a 403 below, so beating pauses until
            # re-registration sets it again.
            await app.state.registered.wait()
            seq += 1
            try:
                status_code = await anyio.to_thread.run_sync(
                    _one_heartbeat, app, seq, abandon_on_cancel=True
                )
                if status_code == 403:
                    diag("heartbeat", level=logging.WARNING, outcome="rejected",
                         status_code=403)
                    logger.warning(
                        "heartbeat rejected (403); pausing + requesting "
                        "re-register"
                    )
                    app.state.registered.clear()
                    app.state.reregister_requested.set()
                elif status_code and status_code >= 400:
                    # A non-403 4xx/5xx is a manager-side problem we can't fix by
                    # re-registering; surface it but keep beating.
                    diag("heartbeat", level=logging.WARNING, outcome="rejected",
                         status_code=status_code)
                elif status_code and status_code < 400:
                    diag("heartbeat", level=logging.DEBUG, outcome="ok",
                         status_code=status_code)
            except asyncio.CancelledError:
                raise
            except (requests.ConnectionError, requests.Timeout):
                diag("heartbeat", outcome="unreachable")
            except Exception:
                logger.exception("heartbeat failed; continuing")
            await asyncio.sleep(settings.heartbeat_interval)
    except asyncio.CancelledError:
        logger.info("heartbeat cancelled")
        raise


def _collect_or_reuse(
    app: FastAPI, settings: Settings
) -> tuple[dict[str, str], bool, list[str], str]:
    """Return ``(status_map, degraded, reasons, captured_at)``. Reuse the
    monitor's recent sweep if fresh (with the sweep's own capture time); else
    collect fresh (capture time = now). On collection failure, return degraded
    with an empty map (the manager must not treat an empty degraded map as
    "everything missing")."""
    now_iso = datetime.now(timezone.utc).isoformat()
    # Reuse the monitor's sweep if it is recent. The monitor refreshes every
    # monitor_interval, so judge freshness against monitor_interval + the
    # heartbeat period: with equal 5s intervals a window of just
    # heartbeat_interval would frequently miss the last tick and re-sweep.
    cached = getattr(app.state, "status_map", None)
    window = settings.monitor_interval + settings.heartbeat_interval
    if cached and (time.monotonic() - cached["at"]) < window:
        return cached["map"], False, [], cached.get("captured_at", now_iso)
    try:
        return collect_status_map(settings), False, [], now_iso
    except Exception:
        logger.exception("heartbeat status collection failed; sending degraded")
        return {}, True, ["docker_unreachable"], now_iso


def _disk_free_bytes(settings: Settings) -> int | None:
    try:
        return shutil.disk_usage(str(settings.greffon_path)).free
    except OSError:
        return None


def _read_meminfo() -> tuple[int | None, int | None]:
    """Host memory ``(used, total)`` in bytes from ``/proc/meminfo``, or
    ``(None, None)``.

    ``/proc`` is host-wide in a container by default (not cgroup-virtualised),
    so this reports the physical machine with no mount or privileged flag.
    ``used = MemTotal - MemAvailable`` (MemAvailable is the kernel's own
    free-plus-reclaimable estimate, the right "used" for a host gauge). Returns
    the pair only when BOTH are present (the manager writes them paired); any
    read/parse failure, or a kernel too old to report MemAvailable, degrades to
    ``(None, None)``. resource-monitoring epic, Feature 1."""
    try:
        fields: dict[str, int] = {}
        # errors="replace" so a stray non-ASCII byte degrades to a parse
        # failure (None) rather than escaping as UnicodeDecodeError.
        with open("/proc/meminfo", encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    fields[key] = int(rest.split()[0]) * 1024  # kB -> bytes
                    if len(fields) == 2:
                        break
    except (OSError, ValueError, IndexError):
        return None, None
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if total is None or available is None:
        return None, None
    used = total - available
    return (used if used >= 0 else 0), total


def _read_cpu_sample() -> tuple[int, int] | None:
    """``(total_jiffies, idle_jiffies)`` from the aggregate ``cpu`` line of
    ``/proc/stat`` (host-wide), or ``None`` on failure. ``total`` is the sum of
    ALL fields (busy plus idle), which is what the busy-percent delta needs;
    ``idle`` folds in iowait. These are monotonic cumulative counters; a busy
    percent needs the delta between two samples (see ``_host_cpu_pct``)."""
    try:
        # errors="replace": a stray non-ASCII byte degrades to None below
        # rather than escaping as UnicodeDecodeError.
        with open("/proc/stat", encoding="ascii", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    parts = first.split()
    # Need "cpu" + at least 5 fields (through idle[3] and iowait[4]); the index
    # access below is outside the try, so this guard is what keeps it safe.
    if len(parts) < 6 or parts[0] != "cpu":
        return None
    try:
        values = [int(v) for v in parts[1:]]
    except ValueError:
        return None
    total = sum(values)
    idle = values[3] + values[4]  # idle + iowait
    return total, idle


def _host_cpu_pct(app: FastAPI) -> float | None:
    """Host-wide busy CPU percent (0..100), as the delta against the previous
    beat's ``/proc/stat`` sample held on ``app.state.cpu_sample``.

    The first beat has no prior sample, so it seeds the baseline and returns
    None (the manager preserves null); subsequent beats compute the delta. The
    heartbeat worker is the only writer and beats run one at a time, so the
    unguarded ``app.state`` read/write is safe. Clamped to 0..100 (the manager
    validates that range). resource-monitoring epic, Feature 1."""
    prev = getattr(app.state, "cpu_sample", None)
    cur = _read_cpu_sample()
    app.state.cpu_sample = cur
    if prev is None or cur is None:
        return None
    delta_total = cur[0] - prev[0]
    delta_idle = cur[1] - prev[1]
    if delta_total <= 0:  # no elapsed jiffies, or a counter reset
        return None
    busy = (delta_total - delta_idle) / delta_total * 100.0
    return round(max(0.0, min(100.0, busy)), 1)


def _one_heartbeat(app: FastAPI, seq: int) -> int:
    """Build and POST one heartbeat. Returns the HTTP status code; network
    errors propagate to the worker loop."""
    settings: Settings = app.state.settings
    status_map, degraded, reasons, captured_at = _collect_or_reuse(app, settings)
    payload = {
        "boot_id": app.state.boot_id,
        "seq": seq,
        "captured_at": captured_at,
        "interval": settings.heartbeat_interval,
        "version": settings.greffer_version,
        "uptime_s": int(time.monotonic() - app.state.started_at),
        "degraded": degraded,
        "reasons": reasons,
        "disk_free_bytes": _disk_free_bytes(settings),
        "instances": status_map,
        # Reported for manager-side DR cert reconciliation (R-DR10): the serial of
        # the cert this greffer currently presents. None until the first install.
        "cert_serial": getattr(app.state, "installed_cert_serial", None),
    }
    # Host vitals (resource-monitoring epic, Feature 1). Read from /proc,
    # independent of the docker status collection above, so they ride even a
    # degraded beat. Any read failure sends null and the manager preserves the
    # last good value.
    mem_used, mem_total = _read_meminfo()
    payload["cpu_pct"] = _host_cpu_pct(app)
    payload["mem_used_bytes"] = mem_used
    payload["mem_total_bytes"] = mem_total
    res = requests.post(
        f"{settings.greffon_base_server}/api/greffer/{settings.greffer_id}/heartbeat/",
        json=payload,
        headers={"X-Greffer-Token": app.state.greffer_token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    return res.status_code
