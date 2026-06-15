"""Per-instance observability digests (resource-monitoring epic, Feature 2).

Strict per-instance container enumeration plus digested one-shot stats and
lazy, TTL-cached disk usage. Every value returned here is a DIGEST: raw daemon
dicts (``cpu_stats``, ``precpu_stats``, raw ``df -v`` output, ...) are never
surfaced. All functions are BLOCKING (Docker SDK / filesystem) and are meant to
be called under the metrics concurrency cap via ``anyio.to_thread.run_sync``
(see the controller endpoints), never directly on the event loop.

Multi-tenancy boundary: ``df -v`` (``client.df()``) is host-wide and returns
every tenant's volumes. The per-instance filter (the anchored ``<id>_`` prefix)
lives entirely here in the digest, so instance A never sees instance B's volume
names or sizes. The manager proxy gates WHICH instance id a user may query; it
does not bound WHAT the greffer returns for that id.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import docker
import requests

from apps.utils.docker.compose import (
    STATUS_IGNORE_LABEL,
    STATUS_IGNORE_VALUE,
    client,
)

logger = logging.getLogger("greffer")

# A short stats cache absorbs rapid re-polls from multiple open tabs; the
# disk walk is far more expensive so it gets a much longer TTL. Both are
# per-instance. ``time.monotonic`` is referenced via the module so tests can
# advance the clock without a real sleep.
_STATS_TTL_SECONDS = 3.0
_DISK_TTL_SECONDS = 60.0
# One host-wide ``df()`` snapshot shared across ALL instances' disk reads
# (``df -v`` walks/sizes every image, container and volume on the host: the
# single most expensive Docker call in the design, O(all host volumes)).
_DF_TTL_SECONDS = 60.0

_DOCKER_ERRORS = (docker.errors.DockerException, OSError,
                  requests.exceptions.RequestException)

_METRIC_KEYS = (
    "cpu_percent", "mem_used_bytes", "mem_limit_bytes",
    "net_rx_bytes", "net_tx_bytes", "blk_read_bytes", "blk_write_bytes",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def instance_data_dir(instance_id: str) -> str:
    """Read-only resolver for the per-instance compose dir. Unlike
    ``compose.get_greffon_path`` it NEVER creates the dir: a metrics read must
    not materialise on-disk state for a never-deployed instance (that would
    defeat the stopped-vs-missing discriminator below)."""
    return os.path.join(os.getenv("GREFFON_PATH", "/data"), instance_id)


def instance_is_deployed(instance_id: str) -> bool:
    """True when the instance has a rendered compose dir, i.e. it is deployed
    (possibly all-stopped); False for a never-deployed/missing instance.

    The committed stopped-vs-missing discriminator (HLD): ``get_status``
    returns empty for BOTH a never-deployed instance and a deployed-but-all-
    stopped one, so container presence cannot distinguish them. The compose
    dir + rendered ``docker-compose.yml`` exist for any deployed instance and
    are absent for a never-deployed one."""
    compose_file = os.path.join(instance_data_dir(instance_id),
                                "docker-compose.yml")
    return os.path.isfile(compose_file)


def list_instance_containers(instance_id: str) -> list:
    """Strict per-instance container enumeration.

    Matches the compose project label ``com.docker.compose.project=<id>`` (an
    EXACT label match, not the name-SUBSTRING ``get_status`` uses, which
    over-matches when one instance UUID prefixes another's container name). The
    project name is pinned to ``<id>`` by the ``-p <id>`` passed on
    ``compose.start``/``compose.stop``.

    Excludes the one-shot init/migrate sidecar by the ``com.greffon.status=
    ignore`` label ONLY, never the unanchored ``'migrate' in name`` substring
    ``_ignore_for_status`` tests first (that would drop a legitimate tenant
    container literally named ``*migrate*``). Accepted limitation: an instance
    from an older, UNLABELLED catalog has no ignore label on its one-shot, so
    that exited sidecar surfaces as a regular (owner-scoped, not cross-tenant)
    container."""
    containers = client.containers.list(
        all=True,
        filters={"label": f"com.docker.compose.project={instance_id}"},
    )
    return [
        c for c in containers
        if (c.labels or {}).get(STATUS_IGNORE_LABEL) != STATUS_IGNORE_VALUE
    ]


def _cpu_percent(stats: dict) -> float:
    """Host-relative busy percent from a single ``stats(stream=False)`` read.

    The daemon carries ``precpu_stats`` in the one snapshot, so the delta needs
    no 1s sleep. Multiplied by ``online_cpus`` so a fully-busy 4-core container
    reads ~400 (NOT clamped to 100: per-container CPU is multi-core, unlike the
    host-aggregate heartbeat figure). A cold-start read (no usable precpu delta)
    returns the sentinel ``0.0`` rather than erroring."""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = (cpu["cpu_usage"]["total_usage"]
                     - pre["cpu_usage"]["total_usage"])
        sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        online = (cpu.get("online_cpus")
                  or len(cpu["cpu_usage"].get("percpu_usage") or [])
                  or 1)
        if sys_delta > 0 and cpu_delta > 0:
            return round((cpu_delta / sys_delta) * online * 100.0, 1)
        return 0.0
    except (KeyError, TypeError, ZeroDivisionError):
        return 0.0


def _mem(stats: dict) -> tuple[int | None, int | None]:
    """``(used, limit)`` bytes. ``used`` subtracts reclaimable page cache
    (``inactive_file``/``cache``) so it reflects working set, not buffered I/O.
    Either is None when the daemon did not report it."""
    try:
        mem = stats["memory_stats"]
        usage = mem.get("usage")
        limit = mem.get("limit")
        if usage is None:
            return None, limit
        sub = mem.get("stats") or {}
        cache = sub.get("inactive_file", sub.get("cache", 0)) or 0
        used = usage - cache
        return (used if used >= 0 else usage), limit
    except (KeyError, TypeError):
        return None, None


def _net(stats: dict) -> tuple[int | None, int | None]:
    """Cumulative ``(rx, tx)`` bytes summed over interfaces, or ``(None, None)``
    when no per-interface stats exist (e.g. host network mode). Rates are
    client-derived from ``captured_at`` deltas; this never sleeps."""
    nets = stats.get("networks")
    if not nets:
        return None, None
    try:
        rx = sum(n.get("rx_bytes", 0) for n in nets.values())
        tx = sum(n.get("tx_bytes", 0) for n in nets.values())
        return rx, tx
    except (AttributeError, TypeError):
        return None, None


def _blk(stats: dict) -> tuple[int | None, int | None]:
    """Cumulative ``(read, write)`` bytes from ``blkio_stats``, or
    ``(None, None)`` when the driver reports none (cgroup v2 hosts often do)."""
    entries = (stats.get("blkio_stats") or {}).get(
        "io_service_bytes_recursive")
    if not entries:
        return None, None
    try:
        read = sum(e["value"] for e in entries
                   if str(e.get("op", "")).lower() == "read")
        write = sum(e["value"] for e in entries
                    if str(e.get("op", "")).lower() == "write")
        return read, write
    except (KeyError, TypeError):
        return None, None


def _null_metrics() -> dict:
    return {k: None for k in _METRIC_KEYS}


def _digest_container(container) -> dict:
    service = (container.labels or {}).get(
        "com.docker.compose.service") or container.name
    entry = {"service": service, "name": container.name,
             "state": container.status}
    # Only a running container carries usable metrics; every other state
    # reports its state with null metrics (a stopped/partial instance is a
    # 200, never a 500).
    if container.status != "running":
        entry.update(_null_metrics())
        return entry
    try:
        raw = container.stats(stream=False)
    except _DOCKER_ERRORS as exc:
        logger.warning("stats_read_failed name=%s err=%s", container.name, exc)
        entry.update(_null_metrics())
        return entry
    used, limit = _mem(raw)
    rx, tx = _net(raw)
    read, write = _blk(raw)
    entry.update(
        cpu_percent=_cpu_percent(raw),
        mem_used_bytes=used,
        mem_limit_bytes=limit,
        net_rx_bytes=rx,
        net_tx_bytes=tx,
        blk_read_bytes=read,
        blk_write_bytes=write,
    )
    return entry


def instance_stats(instance_id: str) -> dict | None:
    """Digested one-shot per-container stats, or ``None`` when the instance is
    not deployed (the caller maps that to missing-on-greffer 404). A
    deployed-but-stopped instance returns each container's state with null
    metrics."""
    if not instance_is_deployed(instance_id):
        return None
    containers = [_digest_container(c)
                  for c in list_instance_containers(instance_id)]
    return {
        "instance_id": instance_id,
        "captured_at": _now_iso(),
        "containers": containers,
    }


_df_lock = threading.Lock()
_df_cache: dict = {"at": 0.0, "volumes": None}


def _host_volumes_snapshot() -> dict[str, int | None]:
    """One host-wide ``client.df()`` shared across all instances' disk reads,
    TTL-cached, so N detail-opens do not trigger N O(all-host-volumes) walks.
    Double-checked under a lock so at most ONE host-wide walk is ever in flight
    even on a cold-cache burst. Returns ``{volume_name: size_or_None}``; size is
    None when the daemon has not computed it (``UsageData.Size`` is ``-1``/
    absent), never a bogus ``0``."""
    now = time.monotonic()
    cached = _df_cache["volumes"]
    if cached is not None and (now - _df_cache["at"]) < _DF_TTL_SECONDS:
        return cached
    with _df_lock:
        now = time.monotonic()
        cached = _df_cache["volumes"]
        if cached is not None and (now - _df_cache["at"]) < _DF_TTL_SECONDS:
            return cached
        df = client.df()
        volumes: dict[str, int | None] = {}
        for vol in df.get("Volumes") or []:
            name = vol.get("Name")
            if not name:
                continue
            usage = vol.get("UsageData") or {}
            size = usage.get("Size")
            volumes[name] = size if isinstance(size, int) and size >= 0 \
                else None
        _df_cache["volumes"] = volumes
        _df_cache["at"] = time.monotonic()
        return volumes


def _app_dir_bytes(instance_id: str) -> int:
    """Apparent size of the bind app/compose dir. The bind dir is greffer-
    visible so this needs no extra privilege; volume CONTENTS are never walked
    (that root is ``0700``), only sized via ``df``.

    The walk is O(files under the bind dir). In practice that dir holds only the
    rendered compose/config (apps write their data into docker VOLUMES, not the
    bind dir), so it is cheap; the 60s disk TTL and the metrics limiter bound how
    often and how concurrently it can run even for a pathological app dir."""
    root = instance_data_dir(instance_id)
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def instance_disk(instance_id: str) -> dict | None:
    """Lazy, digested per-instance disk usage, or ``None`` when not deployed.

    Two classes summed: the bind app dir (cheap ``os.walk``) and the instance's
    docker volumes (sliced out of the shared host-wide ``df`` snapshot by the
    anchored ``<id>_`` prefix). With ``-p <id>`` on ``compose.start`` both
    explicitly-named and compose-auto-prefixed volumes are ``<id>_*``, so the
    anchored prefix is correct for both. If any matched volume's size is
    unavailable, ``volumes_bytes`` and ``total_bytes`` are reported ``None``
    (never a bogus low ``0``)."""
    if not instance_is_deployed(instance_id):
        return None
    app_bytes = _app_dir_bytes(instance_id)
    snapshot = _host_volumes_snapshot()
    prefix = f"{instance_id}_"
    volumes = []
    vol_total = 0
    any_unknown = False
    for name in sorted(snapshot):
        if not name.startswith(prefix):
            continue
        size = snapshot[name]
        volumes.append({"name": name, "bytes": size})
        if size is None:
            any_unknown = True
        else:
            vol_total += size
    volumes_bytes = None if any_unknown else vol_total
    total_bytes = None if volumes_bytes is None else app_bytes + volumes_bytes
    return {
        "instance_id": instance_id,
        "captured_at": _now_iso(),
        "app_dir_bytes": app_bytes,
        "volumes_bytes": volumes_bytes,
        "total_bytes": total_bytes,
        "volumes": volumes,
    }


_stats_cache: dict = {}
_disk_cache: dict = {}


# The per-instance stats/disk caches are deliberately lock-free: under the GIL
# individual dict ops are atomic, so a concurrent cold miss can at worst trigger
# a redundant produce() (an extra Docker fan-out), never corruption. The
# EXPENSIVE call (the host-wide df walk) is the one that is lock-guarded
# (_df_lock) so it is never duplicated; the cheap per-instance reads do not
# warrant a lock.
def _ttl_get(cache: dict, key: str, ttl: float, produce):
    hit = cache.get(key)
    now = time.monotonic()
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    body = produce()
    cache[key] = (now, body)
    return body


def cached_instance_stats(instance_id: str) -> dict | None:
    """Stats behind a short per-instance TTL: two ``GET .../stats/`` within the
    window yield ONE ``container.stats()`` fan-out and the second returns the
    byte-identical cached body."""
    return _ttl_get(_stats_cache, instance_id, _STATS_TTL_SECONDS,
                    lambda: instance_stats(instance_id))


def cached_instance_disk(instance_id: str) -> dict | None:
    """Disk behind a per-instance TTL so a second detail-open inside the window
    does not re-walk; layered over the shared host-wide ``df`` snapshot."""
    return _ttl_get(_disk_cache, instance_id, _DISK_TTL_SECONDS,
                    lambda: instance_disk(instance_id))
