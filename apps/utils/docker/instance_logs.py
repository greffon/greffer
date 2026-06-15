"""Bounded per-instance log reads (resource-monitoring epic, Feature 2, logs
slice).

``stream=container|all|deploy`` with an opaque, server-minted cursor for
de-duplicating follow polls. Surfaces ONLY the target instance's container
stdout/stderr and its captured ``deploy.log``, never the greffer's own process
logs and never another instance's.

Cursor design: container streams use the docker log TIMESTAMP as the position
(``docker logs --timestamps``), compared lexically (RFC3339 sorts
chronologically) so de-dup survives a json-file rotation with no byte-offset
bookkeeping the SDK cannot give us. The merged ``all`` stream carries a
PER-CONTAINER timestamp map (a scalar watermark across interleaved streams
would drop or re-emit lines under clock skew). The ``deploy`` stream is a real
file, so it uses a byte offset and detects a redeploy truncation (the file
shrank below the offset) to reset and flag ``rotated``.

The whole slice is gated by GREFFER_LOG_SURFACING_ENABLED at the source: the
endpoint 404s when off, because container output and especially the deploy log
can echo tenant secrets / registry credentials / pull errors.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime

import docker
import requests

from apps.utils.docker.observe import (
    _now_iso,
    instance_data_dir,
    instance_is_deployed,
    list_instance_containers,
)

logger = logging.getLogger("greffer")

LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 1000

_DOCKER_ERRORS = (docker.errors.DockerException, OSError,
                  requests.exceptions.RequestException)


class BadCursor(ValueError):
    """A client-supplied cursor failed server validation (maps to 400)."""


def clamp_tail(tail) -> int:
    """Clamp the requested tail to ``[1, LOG_TAIL_MAX]``; a missing/garbage
    value falls back to the default. ``tail`` is the upper size safety, never a
    way to drop lines the cursor said were new."""
    try:
        n = int(tail)
    except (TypeError, ValueError):
        return LOG_TAIL_DEFAULT
    return max(1, min(LOG_TAIL_MAX, n))


def _encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str | None) -> dict | None:
    """Validate + decode a cursor the server previously minted. A client never
    constructs one; a malformed/foreign cursor is a 400, not a silent
    full-log read."""
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError):
        raise BadCursor("undecodable cursor")
    if not isinstance(data, dict) or data.get("v") != 1:
        raise BadCursor("unsupported cursor")
    # Type/range-validate every field at this single chokepoint so a forged
    # cursor is a clean 400, never a 500 downstream (int("abc"), seek(-1), or a
    # non-str ts). bool is excluded because it is an int subclass.
    off = data.get("off")
    if off is not None and (not isinstance(off, int) or isinstance(off, bool)
                            or off < 0):
        raise BadCursor("bad offset")
    ts = data.get("ts")
    if ts is not None and not isinstance(ts, str):
        raise BadCursor("bad ts")
    positions = data.get("positions")
    if positions is not None and (
            not isinstance(positions, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in positions.items())):
        raise BadCursor("bad positions")
    return data


def _ts_to_unix(ts_str: str) -> int:
    """Floor an RFC3339 docker timestamp to a unix int for the docker ``since``
    arg (a coarse lower bound; the lexical filter below refines it)."""
    try:
        head = ts_str.split(".", 1)[0].rstrip("Z")
        return int(datetime.fromisoformat(head).timestamp())
    except (ValueError, TypeError):
        return 0


def _parse_container_lines(raw: bytes) -> list[tuple[str, str]]:
    """``[(ts_str, msg)]`` from ``docker logs --timestamps`` output. The
    timestamp is the first whitespace-delimited token on each line."""
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        ts_str, _, msg = line.partition(" ")
        out.append((ts_str, msg))
    return out


def _read_one_container(container, since_ts: str | None, tail: int):
    """Return ``(lines, next_ts, truncated)`` for one container, where ``lines``
    is ``[{service, ts, msg}]`` strictly after ``since_ts``.

    Initial load (no ``since_ts``): the last ``tail`` lines. Follow poll: lines
    after the cursor, clamped to ``tail`` as a size safety (the OLDEST ``tail``
    new lines, so ``next_ts`` lands on the truncation boundary and the next poll
    resumes with no gap)."""
    service = (container.labels or {}).get(
        "com.docker.compose.service") or container.name
    kwargs = {"stdout": True, "stderr": True, "timestamps": True}
    if since_ts:
        kwargs["since"] = _ts_to_unix(since_ts)
    else:
        kwargs["tail"] = tail
    try:
        raw = container.logs(**kwargs)
    except _DOCKER_ERRORS as exc:
        logger.warning("logs_read_failed name=%s err=%s", container.name, exc)
        return [], since_ts, False
    parsed = _parse_container_lines(raw)
    if since_ts:
        # Strict de-dup: drop anything at-or-before the cursor (the coarse
        # int `since` can re-include the boundary second).
        parsed = [(ts, msg) for ts, msg in parsed if ts > since_ts]
    truncated = False
    if len(parsed) > tail:
        parsed = parsed[:tail]
        truncated = True
    next_ts = parsed[-1][0] if parsed else since_ts
    lines = [{"service": service, "ts": ts, "msg": msg}
             for ts, msg in parsed]
    return lines, next_ts, truncated


def _read_deploy(instance_id: str, since_off, tail: int):
    """Return ``(lines, next_off, rotated, truncated)`` for the captured deploy
    log, or ``None`` when the instance has no ``deploy.log`` (never deployed)."""
    path = os.path.join(instance_data_dir(instance_id), "deploy.log")
    if not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
        off = int(since_off or 0)
        rotated = False
        if off > size:
            # A redeploy truncated the file ('wb'); resume from the new start.
            off = 0
            rotated = True
        with open(path, "rb") as fh:
            fh.seek(off)
            data = fh.read()
    except OSError as exc:
        logger.warning("deploy_log_read_failed id=%s err=%s", instance_id, exc)
        return [], since_off or 0, False, False
    # Only emit COMPLETE lines (terminated by \n); a trailing partial waits for
    # the next poll. Byte-precise so the offset never skips/repeats a line.
    raw_lines = data.split(b"\n")[:-1]
    truncated = False
    if len(raw_lines) > tail:
        raw_lines = raw_lines[:tail]
        truncated = True
    consumed = sum(len(b) + 1 for b in raw_lines)
    next_off = off + consumed
    lines = [{"service": "deploy", "ts": None,
              "msg": b.decode("utf-8", errors="replace")} for b in raw_lines]
    return lines, next_off, rotated, truncated


def instance_logs(instance_id: str, stream: str, tail, since: str | None):
    """Digested bounded log read, or ``None`` when the instance is not deployed
    AND has no deploy log (the caller maps that to missing-on-greffer 404).

    ``stream``: ``container``/``all`` (merged container stdout/stderr) or
    ``deploy`` (the captured deploy log)."""
    tail = clamp_tail(tail)
    cursor = decode_cursor(since)
    deployed = instance_is_deployed(instance_id)

    if stream == "deploy":
        result = _read_deploy(
            instance_id, (cursor or {}).get("off"), tail)
        if result is None:
            return None if not deployed else {
                "instance_id": instance_id, "stream": "deploy",
                "captured_at": _now_iso(), "lines": [],
                "next_cursor": since, "rotated": False, "truncated": False}
        lines, next_off, rotated, truncated = result
        return {
            "instance_id": instance_id, "stream": "deploy",
            "captured_at": _now_iso(), "lines": lines,
            "next_cursor": _encode_cursor({"v": 1, "off": next_off}),
            "rotated": rotated, "truncated": truncated,
        }

    # container / all: never-deployed => missing (404). A deployed-but-stopped
    # instance returns its retained container output (possibly empty). Both
    # streams use the SAME per-container position map: a scalar watermark across
    # interleaved containers would drop a lagging container's genuinely-new
    # lines (the exact hazard the per-container map exists to prevent), so the
    # ``container`` stream must not collapse to one ts. v1 ``container`` is thus
    # the merged view; a future ``service=`` selector narrows it to one source.
    if not deployed:
        return None
    containers = list_instance_containers(instance_id)
    positions = (cursor or {}).get("positions") or {}
    all_lines = []
    # Built fresh from the CURRENT containers, so a service that disappeared
    # between polls is pruned from the cursor (no unbounded growth).
    new_positions = {}
    truncated = False
    for c in containers:
        service = (c.labels or {}).get(
            "com.docker.compose.service") or c.name
        lines, next_ts, trunc = _read_one_container(
            c, positions.get(service), tail)
        all_lines.extend(lines)
        truncated = truncated or trunc
        if next_ts:
            new_positions[service] = next_ts
    # Merge by timestamp (RFC3339 sorts chronologically); None-safe.
    all_lines.sort(key=lambda r: r["ts"] or "")
    next_cursor = _encode_cursor({"v": 1, "positions": new_positions}) \
        if new_positions else since
    return {
        "instance_id": instance_id, "stream": stream,
        "captured_at": _now_iso(), "lines": all_lines,
        "next_cursor": next_cursor, "rotated": False, "truncated": truncated,
    }
