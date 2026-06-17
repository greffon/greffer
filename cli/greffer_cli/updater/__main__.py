"""Entrypoint for the signed ``greffon/greffer-updater`` image (v2 ``:latest``).

``python -m greffer_cli.updater`` (no positional args: the model is "update to
latest"; the rest comes from the env the greffer sets when it spawns the
updater). It takes the ``/data`` update lock so a remote update and a host
``greffer update`` are mutually exclusive (HLD section 10), runs the
verify-then-pull -> recreate -> ``/readyz`` gate engine, and exits with its code.

The lock is ``/data/.update.lock`` on the greffer's persistent ``/data`` volume,
the same host inode a host ``greffer update`` flocks via the volume mountpoint.
``/data`` is the only host path mounted into the updater (socket-only, no compose
dir), and the filename matches v1's (``.update.lock``) so the two actually
contend (HLD section 10).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import engine

# Paths inside the updater container: the greffer mounts the docker socket and
# its /data volume; cosign.pub is baked into /etc/greffer at build time.
DEFAULT_LOCK = Path("/data/.update.lock")
DEFAULT_COSIGN_PUB = "/etc/greffer/cosign.pub"

# Sentinel: no fcntl (non-POSIX) -> proceed without a host lock.
_NO_LOCK = object()


def _config_from_env(env: dict) -> dict:
    """The socket-model config: a server-resolved ``target_tag`` (the controller
    sets ``GREFFER_UPDATER_TARGET_TAG``; absent -> ``latest``), no manifest /
    floor / compose path. All keys have defaults, so this never raises."""
    return {
        "cosign_pub": env.get("GREFFER_COSIGN_PUB", DEFAULT_COSIGN_PUB),
        "greffer_id": env.get("GREFFER_ID"),
        "target_tag": env.get("GREFFER_UPDATER_TARGET_TAG") or None,
        "timeout": float(env.get("GREFFER_UPDATER_TIMEOUT", "600")),
    }


def acquire_lock(lock_path: Path = DEFAULT_LOCK):
    """Exclusive flock on ``/data/.update.lock``. Returns a handle to release, or
    None if another actor holds it (the caller refuses), or ``_NO_LOCK`` on a
    platform without fcntl."""
    try:
        import fcntl
    except ImportError:
        return _NO_LOCK
    fh = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def release_lock(handle) -> None:
    if handle is None or handle is _NO_LOCK:
        return
    try:
        handle.close()  # closing the fd releases the flock
    except OSError:
        pass


def main(argv=None, *, env=None, run=engine.run_remote_update,
         lock_acquire=acquire_lock, lock_release=release_lock) -> int:
    """Resolve config, take the ``/data`` lock, run the engine. ``run`` /
    ``lock_acquire`` are injectable for tests. ``argv`` is ignored (no
    positional args in the ``:latest`` model)."""
    env = os.environ if env is None else env
    cfg = _config_from_env(env)
    handle = lock_acquire()
    if handle is None:
        print("another update is in progress (/data lock held)", file=sys.stderr)
        return engine.EXIT_REFUSED
    try:
        return run(**cfg)
    finally:
        lock_release(handle)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
