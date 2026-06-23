"""Cross-instance L4 host-port reservation via the docker daemon.

The greffer gives each L4 (Tier-C) instance a host port from a dedicated range.
It used to decide "is this port free" with ``socket.bind`` inside its OWN
container network namespace, which is blind to ports the docker daemon
publishes on the HOST: a port already bound on the host reads as free from
inside the greffer container, so two L4 instances were handed the same number
and the second container failed to bind (``port is already allocated``), then
its nginx sidecar crash-looped on the missing upstream.

The daemon is the source of truth. A host port is reserved only while a
container is RUNNING (a ``created``/``exited`` container releases it), so
enumerating running containers' published ports is the accurate, self-cleaning
view of what is actually bound. This module provides that enumeration plus a
pure range pick, and the allocation errors the controller turns into clean HTTP
responses.
"""
import logging
import math
import os
import threading
import time

import docker
import requests
from docker.errors import DockerException

logger = logging.getLogger("greffer")

# The enumerator runs stateless ``list()`` reads; give it its own client rather
# than sharing the compose path's (docker-py tolerates concurrent stateless
# reads across clients). A short timeout so a HUNG (not just down) daemon fails
# fast to L4PortsUnavailable instead of pinning the allocation lock for docker-py's
# default 60s (a normal list() is sub-second).
client = docker.from_env(timeout=30)

_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


# --- In-process allocation guard (concurrency) -----------------------------
# FastAPI runs the sync start handler in a threadpool, so two starts of two
# DIFFERENT instances are real threads in one greffer process. Without a guard
# they can both read the daemon, both see a port free, and both pick it before
# either container has bound it (the old socket-probe allocator excluded
# concurrent threads as a side effect of holding probe sockets open; the daemon
# read does not). ``allocation_lock`` serialises the read -> pick -> reserve
# decision (NOT the later docker-compose up, which must stay parallel), and
# ``_pending`` holds ports handed out but not yet visible in the daemon's
# running set, so the second thread's reserved set includes the first thread's
# just-picked port. A pending entry is dropped once the port shows up as
# occupied (its container bound it) or after a generous TTL that bounds the leak
# from a start that never binds (a pull exceeding the TTL only reopens the
# pre-fix window, never worse than today).
_alloc_lock = threading.Lock()
_pending = {}  # proto -> {port: (expiry_monotonic, owner_instance_id)}


def _pending_ttl_seconds():
    # A bad tunable must not crash the greffer at import, and must NOT silently
    # disable the guard: a non-positive TTL would expire every reservation
    # instantly and reopen the very race the pending set closes. Fall back to
    # the default and warn instead.
    raw = os.getenv("GREFFER_L4_PENDING_TTL_SECONDS", "300")
    try:
        val = float(raw)
    except ValueError:
        logger.warning("invalid GREFFER_L4_PENDING_TTL_SECONDS=%r; using 300", raw)
        return 300.0
    if not math.isfinite(val) or val <= 0:
        logger.warning(
            "non-finite/non-positive GREFFER_L4_PENDING_TTL_SECONDS=%s; using 300",
            val)
        return 300.0
    return val


_PENDING_TTL_SECONDS = _pending_ttl_seconds()


def allocation_lock():
    """The lock to hold across enumerate -> pick -> mark_pending."""
    return _alloc_lock


def pending_and_prune(occupied, exclude_instance):
    """Drop expired or now-daemon-visible reservations and return the ports
    reserved by OTHER instances as ``{proto: set(int)}`` to merge into the
    reserved set.

    Entries owned by ``exclude_instance`` are kept (its own running container is
    excluded from ``occupied``, so its entry would never prune for itself) but
    NOT returned against it, so an instance is never blocked from reclaiming its
    own port on a restart within the TTL. Call under ``allocation_lock``."""
    now = time.monotonic()
    others = {}
    for proto in list(_pending):
        occ = occupied.get(proto, set())
        kept = {}
        reserved = set()
        for port, (exp, owner) in _pending[proto].items():
            if exp <= now or port in occ:
                continue
            kept[port] = (exp, owner)
            if owner != exclude_instance:
                reserved.add(port)
        if kept:
            _pending[proto] = kept
            if reserved:
                others[proto] = reserved
        else:
            _pending.pop(proto, None)
    return others


def mark_pending(instance_id, proto, port):
    """Reserve ``port`` for ``instance_id`` until its container is daemon-visible
    or the TTL lapses. Call under ``allocation_lock``."""
    _pending.setdefault(proto, {})[port] = (
        time.monotonic() + _PENDING_TTL_SECONDS, instance_id)


class L4PortError(Exception):
    """Base for L4 host-port allocation failures."""


class L4SamePortConflict(L4PortError):
    """A ``same_port`` instance's pinned host port is taken, on a greffer where
    it cannot be rotated.

    Proxy mode only: the published host port IS the advertised/listen port the
    app baked into client configs, so rotating it would silently break every
    client. Surfaced loudly instead. (Tunnel mode rotates freely because the
    host port there is only the loopback port the rathole-client dials.)
    """

    def __init__(self, port_name, port):
        self.port_name = port_name
        self.port = port
        super().__init__(
            f"l4_same_port_conflict: host port {port} for {port_name} is held "
            f"by another instance and cannot be rotated for a proxy same_port "
            f"endpoint")


class L4PortRangeExhausted(L4PortError):
    def __init__(self, range_start, range_end):
        self.range_start = range_start
        self.range_end = range_end
        super().__init__(
            f"l4_port_range_exhausted: no free host port in "
            f"{range_start}-{range_end}")


class L4PortsUnavailable(L4PortError):
    """The docker daemon could not be enumerated for published ports.

    We do NOT degrade to "nothing is reserved": that blindly reissues the
    bottom of the range and reintroduces the collision. The start fails cleanly.
    """


def published_l4_ports(range_start, range_end, exclude_project=None):
    """Host ports in ``[range_start, range_end]`` published by RUNNING
    containers, as ``{protocol: set(int)}``.

    ``HostIp`` is collapsed (a ``0.0.0.0`` and a ``127.0.0.1`` publish of the
    same number both count). Containers belonging to ``exclude_project`` (the
    compose project name, which the greffer pins to the instance id via
    ``-p <id>``) are skipped, so re-deploying an instance keeps its own ports.
    Raises :class:`L4PortsUnavailable` if the daemon cannot be reached.
    """
    try:
        # ``sparse=True`` keeps the ``/containers/json`` payload (top-level
        # ``Ports``/``Labels``) instead of inspecting each container; the default
        # ``sparse=False`` makes ``attrs`` the inspect shape (ports under
        # ``NetworkSettings.Ports`` as a dict, labels under ``Config.Labels``),
        # which this parse does not read — it would silently see no ports and
        # reintroduce the collision. ``all`` defaults False, so this is
        # running-only (a host port is reserved only while a container runs).
        containers = client.containers.list(sparse=True)
    except (DockerException, OSError,
            requests.exceptions.RequestException) as exc:
        # A down/unreachable daemon surfaces as a docker error, a raw requests
        # connection error, OR a bare OSError (socket gone mid-call). Mirrors
        # apps/utils/docker/observe.py ``_DOCKER_ERRORS``.
        raise L4PortsUnavailable(
            f"l4_port_enumeration_failed: {exc}") from exc
    occupied = {}
    for container in containers:
        attrs = container.attrs or {}
        if exclude_project is not None:
            labels = attrs.get("Labels") or {}
            if labels.get(_COMPOSE_PROJECT_LABEL) == exclude_project:
                continue
        # ``/containers/json`` lists published ports as
        # ``[{"PrivatePort":.., "PublicPort":.., "Type":"tcp"|"udp", "IP":..}]``.
        # Only published entries carry a ``PublicPort``.
        for spec in attrs.get("Ports") or []:
            if not isinstance(spec, dict):
                continue
            public = spec.get("PublicPort")
            if public is None:
                continue
            public = int(public)
            if range_start <= public <= range_end:
                proto = (spec.get("Type") or "tcp").lower()
                occupied.setdefault(proto, set()).add(public)
    return occupied


def lowest_free_port(range_start, range_end, taken):
    """Lowest port in ``[range_start, range_end]`` not in ``taken`` (or None)."""
    for candidate in range(range_start, range_end + 1):
        if candidate not in taken:
            return candidate
    return None
