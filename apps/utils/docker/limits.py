"""Per-service resource limits for greffon instances (compose-containment
Feature 4, stage 1: TELEMETRY ONLY).

The HLD (docs/features/compose-containment/hld.md) commits to a two-stage
rollout because no measurement history exists: stage 1 computes the limits and
warns when observed usage exceeds what enforcement would allow, so one release
of telemetry answers "would 2g / 2.0 cpus OOM-kill a real catalog app"; stage 2
renders ``mem_limit`` / ``cpus`` / ``pids_limit`` into every service the way
``_inject_instance_log_rotation`` does (author-declared values win).

Nothing in this module constrains a container yet. It must not grow a render
side effect until the stage-2 decision is taken with telemetry in hand.

Env knobs (they also bind Settings fields of the same names — the log-rotation
precedent: Settings documents and validates them, this module reads the env the
way ``_inject_instance_log_rotation`` does):
``GREFFER_INSTANCE_MEM_LIMIT`` (docker size string, default ``2g``),
``GREFFER_INSTANCE_CPUS`` (float, default ``2.0``),
``GREFFER_INSTANCE_PIDS`` (int, default ``512``).
``0`` disables a dimension on any knob, and an empty string disables MEM
(both the docker ``--memory=0``=unlimited reading); an empty CPUS/PIDS is
unparseable and falls back loudly to the default. Disabled is the semantics
stage 2 will reuse for "no limit", so operators can pre-configure their
nodes.
"""

from __future__ import annotations

import logging
import math
import os
import time

logger = logging.getLogger("greffer")

# Re-warn at most this often per (instance, service): the digest is pull-driven
# (every manager-side metrics poll is one observation), so an unbounded warn
# per poll would spam the log for a steady over-limit app.
_REWARN_SECONDS = 600.0

_SIZE_SUFFIXES = {
    "b": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}


def parse_size(value: str) -> int | None:
    """Parse a docker-style size ("512", "10m", "2g", "1.5g") into bytes.

    ``None`` for anything unparseable (including compound "1g512m", which
    docker's own parser accepts — callers here only ever need single-suffix
    values, and refusing rather than half-parsing keeps a typo loud instead of
    silently smaller). Never raises: an operator typo must degrade to the
    default, not to a crash (the ``_coerce_log_max_file`` precedent).
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    # Docker accepts two-letter suffixes (kb/mb/gb/tb) as well as single
    # (k/m/g/t); '512mb' must parse as 512 MiB, not fall back to the default
    # (which would silently disable the operator's threshold) — codex review.
    number = text
    multiplier = 1
    if number[-1] == 'b' and len(number) > 1:
        number = number[:-1]
    if number and number[-1] in _SIZE_SUFFIXES:
        multiplier = _SIZE_SUFFIXES[number[-1]]
        number = number[:-1]
    if not number:
        return None
    try:
        size = float(number)
    except ValueError:
        return None
    # Check the PRODUCT, not the operand: "1e999g" parses to inf directly,
    # and "1e300g" is a finite float whose g-scaled product overflows to inf
    # — int(inf) raises OverflowError out of this "never raises" function,
    # which once escaped the Settings validator and crash-looped boot.
    scaled = size * multiplier
    if not math.isfinite(scaled) or scaled < 0:
        return None
    return int(scaled)


class InstanceLimits:
    """The per-node computed limit set. ``None`` members mean "dimension
    disabled" (operator set '' / 0), not "unknown"."""

    __slots__ = ("cpus", "mem_bytes", "pids")

    def __init__(self, mem_bytes: int | None, cpus: float | None, pids: int | None):
        self.mem_bytes = mem_bytes
        self.cpus = cpus
        self.pids = pids


# The single source of truth for the three defaults: app/settings.py binds
# its field defaults AND validator fallbacks to these, so the Settings view
# and the env view cannot drift (they are two readers of one value, the
# log-rotation precedent's shape with the duplication removed).
DEFAULT_MEM_LIMIT = "2g"
DEFAULT_CPUS = "2.0"
DEFAULT_PIDS = "512"

# Parsed once per process: env does not change under a running greffer, and
# the hook below runs on every stats digest, so it must stay allocation-free.
_limits_cache: InstanceLimits | None = None


def computed_instance_limits() -> InstanceLimits:
    """Read + parse the three knobs, falling back to defaults on typos (with
    a warning naming the bad value), and cache the result."""
    global _limits_cache
    if _limits_cache is not None:
        return _limits_cache

    raw_mem = os.getenv("GREFFER_INSTANCE_MEM_LIMIT", DEFAULT_MEM_LIMIT)
    mem = parse_size(raw_mem)
    if not raw_mem.strip():
        mem = None  # explicit off
    elif mem is None:
        logger.warning(
            "invalid GREFFER_INSTANCE_MEM_LIMIT=%r; using %s",
            raw_mem,
            DEFAULT_MEM_LIMIT,
        )
        mem = parse_size(DEFAULT_MEM_LIMIT)
    elif mem == 0:
        mem = None  # explicit off — docker's own --memory=0 means unlimited;
        # a literal 0-byte limit would warn on every container and poison the
        # stage-2 calibration data.

    raw_cpus = os.getenv("GREFFER_INSTANCE_CPUS", DEFAULT_CPUS)
    try:
        cpus = float(raw_cpus)
        if not math.isfinite(cpus) or cpus < 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(
            "invalid GREFFER_INSTANCE_CPUS=%r; using %s", raw_cpus, DEFAULT_CPUS
        )
        cpus = float(DEFAULT_CPUS)
    if cpus == 0.0:
        cpus = None  # explicit off

    raw_pids = os.getenv("GREFFER_INSTANCE_PIDS", DEFAULT_PIDS)
    try:
        pids = int(raw_pids)
        if pids < 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(
            "invalid GREFFER_INSTANCE_PIDS=%r; using %s", raw_pids, DEFAULT_PIDS
        )
        pids = int(DEFAULT_PIDS)
    if pids == 0:
        pids = None  # explicit off

    _limits_cache = InstanceLimits(mem, cpus, pids)
    return _limits_cache


# (instance_id, service) -> monotonic time of the last warning. Thread-safety
# mirrors observe's cpu-prev map (snapshot-before-delete, benign races), NOT a
# lock: the digest runs on the bounded metrics threadpool, so concurrent
# instance_stats calls are real threads and a live-view iteration could raise.
_last_warned: dict[tuple[str, str], float] = {}


def _prune_last_warned(now: float) -> None:
    """Bound the map: entries whose re-warn window has lapsed are droppable,
    and the digest is already pull-frequency-bounded. The list() snapshot is
    load-bearing: popping while iterating the live view raises
    ``RuntimeError: dictionary changed size during iteration`` when a
    concurrent digest records a new warn key."""
    stale = [k for k, t in list(_last_warned.items()) if now - t >= _REWARN_SECONDS]
    for key in stale:
        _last_warned.pop(key, None)


def check_limits_observed(
    instance_id: str,
    digest: list[dict],
    limits: InstanceLimits | None = None,
    *,
    now: float | None = None,
) -> int:
    """Stage-1 telemetry: compare observed per-container usage against the
    computed limits and WARN when enforcement would have constrained.

    Called from ``observe.instance_stats`` on every digested stats read, so
    observations accumulate exactly as often as metrics are pulled. Warnings
    are rate-limited per (instance, service) to ``_REWARN_SECONDS``. Returns
    the number of currently-exceeding containers (0 when nothing exceeds or
    the dimension is disabled) — the return value exists for tests and for a
    future ``over_limit`` heartbeat field; nothing here blocks or rewrites.

    Only memory and CPU are observable today (the digest carries
    ``mem_used_bytes`` / ``cpu_percent``); pids is carried in the computed set
    for stage 2 but has no observation channel, so it is deliberately not
    compared — a pretend measurement would be worse than none.
    """
    if limits is None:
        limits = computed_instance_limits()
    if limits.mem_bytes is None and limits.cpus is None:
        return 0
    clock = time.monotonic() if now is None else now
    _prune_last_warned(clock)

    exceeding = 0
    for entry in digest:
        mem_used = entry.get("mem_used_bytes")
        cpu_pct = entry.get("cpu_percent")
        over_mem = (
            limits.mem_bytes is not None
            and isinstance(mem_used, (int, float))
            and mem_used > limits.mem_bytes
        )
        # cpu_percent is host-relative and multi-core (a fully-busy 4-core
        # container reads ~400), so the comparison is against cpus * 100.
        over_cpu = (
            limits.cpus is not None
            and isinstance(cpu_pct, (int, float))
            and cpu_pct > limits.cpus * 100.0
        )
        if not (over_mem or over_cpu):
            continue
        exceeding += 1
        service = entry.get("service") or entry.get("name") or "unknown"
        key = (instance_id, service)
        # An explicit membership test, not a 0.0 sentinel: monotonic clocks
        # start near 0 at boot, so `clock - 0.0 < 600` would silently swallow
        # every first warning during the first ten minutes after a host boot.
        last = _last_warned.get(key)
        if last is not None and clock - last < _REWARN_SECONDS:
            continue
        _last_warned[key] = clock
        logger.warning(
            "instance_limit_exceeded instance_id=%s service=%s "
            "mem_used=%s mem_limit=%s cpu_percent=%s cpu_limit_percent=%s "
            "(telemetry only, not enforced; stage-2 calibration signal)",
            instance_id,
            service,
            mem_used if over_mem else "-",
            limits.mem_bytes if over_mem else "-",
            cpu_pct if over_cpu else "-",
            round(limits.cpus * 100.0, 1) if over_cpu else "-",
        )
    return exceeding
