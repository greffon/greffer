"""Greffer-side writer for the manager-pushed rathole client.toml.

Part of tunnel-support epic v3 (§"Changes from v2" §4). The manager
embeds the rendered ``client.toml`` in:

- ``GET /api/greffer/certificate/{id}/`` response body — initial config,
  consumed by the register-worker on accept (see
  ``app/workers/register.py``).
- ``POST /api/controller/start/`` and ``POST /api/controller/stop/``
  request bodies — full file with current instance services (see
  ``app/routers/controller.py``).

This module is the single point that touches the file system so all
writes go through one atomic rename and one shape of error reporting.

Atomicity matters: concurrent start + stop on the same greffer (rare
but possible — different greffon instances racing) must not produce
torn writes. ``os.replace`` on POSIX is atomic at the directory entry
level, so rathole-client's file-watcher always sees either the old
file or the new file, never a half-written one. The diff-based
hot-reload (verified in the v2 spike) keeps existing connections on
unchanged services across the swap.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("greffer")


class TunnelConfigWriteError(OSError):
    """Wraps the underlying OSError so the caller can map to the
    ``config_write_status='failed'`` shape the API contract specifies."""


def write_client_toml(content: str, target_path: str | os.PathLike[str]) -> None:
    """Atomically write ``content`` to ``target_path``.

    Writes to a temp file in the same directory (so the rename is
    same-filesystem), fsyncs, then renames into place. Raises
    ``TunnelConfigWriteError`` on any OS-level failure — directory
    missing, permission denied, disk full, etc.

    Caller is responsible for the "should I write?" decision (e.g.
    skipping when ``settings.greffer_tunnel_client_config_path`` is
    empty or when the payload didn't include ``tunnel_client_toml``).
    """
    target = Path(target_path)
    target_dir = target.parent

    # mkstemp in the same directory keeps os.replace cross-fs-safe.
    # delete=False because we manage the lifetime via os.replace.
    fd = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target_dir),
        )
        # Use os.fdopen so the FD we already have is the one we write +
        # fsync against; opening by name would race with another writer.
        with os.fdopen(fd, "w") as f:
            fd = None  # ownership transferred to the with-block
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        tmp_path = None  # rename succeeded; nothing to clean up
        logger.info("tunnel_client_toml_written path=%s bytes=%d",
                    target, len(content))
    except OSError as exc:
        logger.error("tunnel_client_toml_write_failed path=%s error=%s",
                     target, exc)
        raise TunnelConfigWriteError(
            f"failed to write {target}: {exc}"
        ) from exc
    finally:
        # Best-effort cleanup of leftover temp file on any failure path
        # (mkstemp succeeded but write/replace didn't). Swallow errors —
        # the original exception, if any, is what the caller cares about.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def maybe_write_client_toml(
    content: str | None,
    target_path: str | os.PathLike[str],
) -> bool:
    """Convenience wrapper used by start/stop handlers.

    Returns True iff a write was attempted AND succeeded. Returns False
    when ``content`` is None (proxy-mode greffer or v2-manager pushing
    no field) or ``target_path`` is empty (path disabled). Raises
    ``TunnelConfigWriteError`` if write was attempted but failed —
    callers should map that to ``config_write_status='failed'`` and
    let the manager surface it to the API caller.
    """
    if content is None or not target_path:
        return False
    write_client_toml(content, target_path)
    return True
