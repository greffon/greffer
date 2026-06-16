"""Entrypoint for the signed ``greffon/greffer-updater`` image.

``python -m greffer_cli.updater <target_tag>`` (the rest of the config comes
from the environment the greffer sets when it spawns the updater). It takes the
update lock so a remote update and a host ``greffer update`` are mutually
exclusive (HLD "Concurrency"), runs the v2 verify -> pin -> recreate engine, and
exits with its code.

The lock file is ``/work/.update.lock``. ``/work`` is the host compose dir
bind-mounted into the updater, which is exactly the dir a host ``greffer
update`` locks (``<config_dir>/.update.lock`` in greffer_cli.update). Both sides
must lock the SAME host inode for ``flock`` to actually serialize them; locking
``/data`` instead would be a different inode and the two updaters could run
concurrently and corrupt the shared compose file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import engine

# Paths inside the updater container: the greffer mounts the host config dir at
# /work, the greffer's /data volume at /data, and bakes cosign.pub + the
# min_supported baseline into /etc/greffer at build time.
DEFAULT_COMPOSE = Path("/work/docker-compose.yml")
DEFAULT_RATCHET = Path("/data/.greffer-update-floor")
# Same host inode a host ``greffer update`` locks (its ``<config_dir>/
# .update.lock``); /work IS that config dir bind-mounted in. Must match the v1
# filename ``.update.lock`` or the two locks miss each other (P1).
DEFAULT_LOCK = Path("/work/.update.lock")
DEFAULT_COSIGN_PUB = "/etc/greffer/cosign.pub"
DEFAULT_BASELINE_FILE = Path("/etc/greffer/min_supported_baseline")

# Sentinel: no fcntl (non-POSIX); proceed without a host lock.
_NO_LOCK = object()


def _baked_baseline(env: dict) -> str | None:
    """The build-time min_supported baseline, from an env override or the file
    baked into the image. None if neither is present (the floor then rests on
    the signed manifest + ratchet)."""
    v = env.get("GREFFER_MIN_SUPPORTED_BASELINE")
    if v and v.strip():
        return v.strip()
    try:
        return DEFAULT_BASELINE_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _config_from_env(target_tag: str, env: dict) -> dict:
    return {
        "compose_file": Path(env.get("GREFFER_UPDATER_COMPOSE", str(DEFAULT_COMPOSE))),
        "target_tag": target_tag,
        "manifest_url": env["GREFFER_VERSION_MANIFEST_URL"],
        "cosign_pub": env.get("GREFFER_COSIGN_PUB", DEFAULT_COSIGN_PUB),
        "baked_baseline": _baked_baseline(env),
        "ratchet_path": Path(env.get("GREFFER_UPDATER_RATCHET", str(DEFAULT_RATCHET))),
        "greffer_id": env.get("GREFFER_ID"),
        "mode": env.get("GREFFER_MODE") or "proxy",
        "timeout": float(env.get("GREFFER_UPDATER_TIMEOUT", "600")),
    }


def acquire_lock(lock_path: Path = DEFAULT_LOCK):
    """Exclusive flock on the /work update lock. Returns a handle to release, or
    None if another actor already holds it (the caller refuses), or the
    ``_NO_LOCK`` sentinel on a platform without fcntl."""
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
    """Resolve config, take the /work lock, run the engine. ``run`` /
    ``lock_acquire`` are injectable for tests."""
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if len(argv) != 1 or not argv[0]:
        print("usage: python -m greffer_cli.updater <target_tag>", file=sys.stderr)
        return engine.EXIT_REFUSED
    try:
        cfg = _config_from_env(argv[0], env)
    except KeyError as exc:
        print(f"missing required updater env: {exc}", file=sys.stderr)
        return engine.EXIT_REFUSED

    handle = lock_acquire()
    if handle is None:
        print("another update is in progress (/work lock held)", file=sys.stderr)
        return engine.EXIT_REFUSED
    try:
        return run(**cfg)
    finally:
        lock_release(handle)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
