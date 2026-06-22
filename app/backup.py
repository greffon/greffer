"""Greffon cold backup / restore engine (backup-restore Phase 1).

The greffer holds the restic repo + creds; the manager never touches bytes. A
controller call spawns a background THREAD (a sync handler cannot create an
asyncio task) that:

- takes an IN-PROCESS per-instance lock (serializes vs a concurrent user
  start/stop in the single greffer process -- a file lock would NOT, flock is
  per-fd/cross-process; the file lock is only the cross-process updater
  interlock),
- stops the instance and WAITS for quiescence (``compose.stop`` is
  fire-and-forget),
- restic-snapshots the DATA volumes (only -- the nginx volume + instance dir are
  regenerated on start) via a digest-pinned sidecar,
- restarts (cold) in a ``try/finally`` so a running instance always comes back,
- posts the result to the manager.

Restore mirrors it, but takes a SAFETY snapshot of the stopped instance first
(the reversibility net), overwrites the volumes, leaves the instance stopped, and
the manager runs the normal start flow on the success callback.

``error_code`` values are a closed taxonomy the manager re-validates against its
enum -- raw restic stderr is never sent.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

import requests

from apps.utils.docker import compose

logger = logging.getLogger(__name__)

# In-process per-instance locks (HLD section 3).
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}

_HTTP_TIMEOUT = 30
_NGINX_VOLUME_SUFFIX = "_nginx_volume"


class BusyError(Exception):
    """The instance is already mid-op in this greffer (409 instance_busy)."""


class BackupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _instance_lock(instance_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(instance_id)
        if lock is None:
            lock = threading.Lock()
            _locks[instance_id] = lock
        return lock


def restic_env(settings) -> dict:
    """Repo + password + S3 creds reach restic via env ONLY (never argv/logged)."""
    if not settings.greffer_backup_repo:
        raise BackupError("repo_uninitialized")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "RESTIC_REPOSITORY": settings.greffer_backup_repo,
    }
    if settings.restic_password_file:
        env["RESTIC_PASSWORD_FILE"] = settings.restic_password_file
    elif settings.restic_password:
        env["RESTIC_PASSWORD"] = settings.restic_password
    else:
        raise BackupError("repo_uninitialized")
    if settings.aws_access_key_id:
        env["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        env["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
    return env


def _data_volumes(instance_id: str) -> list[str]:
    """The instance's DATA volumes (``<id>_*``), excluding the regenerated
    ``<id>_nginx_volume``."""
    vols = compose.client.volumes.list(filters={"name": instance_id})
    return sorted(
        v.name for v in vols
        if v.name.startswith(instance_id) and not v.name.endswith(_NGINX_VOLUME_SUFFIX)
    )


def _classify(stderr: str) -> str:
    text = (stderr or "").lower()
    if "wrong password" in text or "invalid password" in text:
        return "auth_failed"
    if "unable to open config" in text or "does not exist" in text:
        return "repo_uninitialized"
    if any(k in text for k in ("no space", "disk full", "out of space")):
        return "disk_full"
    if any(k in text for k in ("connection refused", "timeout", "no route", "dial tcp")):
        return "repo_unreachable"
    return "snapshot_failed"


def _run_restic(settings, args: list[str], mounts: list[str], *, read_only: bool) -> tuple[int, str, str]:
    """Run the digest-pinned restic sidecar with the data volumes mounted under
    ``/data`` and the repo creds in env. Returns (rc, stdout, stderr)."""
    docker_args = ["docker", "run", "--rm"]
    for key, value in restic_env(settings).items():
        docker_args += ["-e", f"{key}={value}"]
    suffix = ":ro" if read_only else ""
    for vol in mounts:
        # strip the "<id>_" prefix for a stable, collision-free mount path
        docker_args += ["-v", f"{vol}:/data/{vol}{suffix}"]
    docker_args += ["--entrypoint", "restic", settings.restic_sidecar_image, *args]
    proc = subprocess.run(docker_args, capture_output=True, text=True, timeout=3600)
    return proc.returncode, proc.stdout, proc.stderr


def _wait_stopped(instance_id: str, timeout: int) -> bool:
    """Poll until all the instance's containers are stopped (``compose.stop`` is
    fire-and-forget). Returns True if quiesced within the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if compose.get_status(instance_id).get("status") == "stopped":
            return True
        time.sleep(1.0)
    return compose.get_status(instance_id).get("status") == "stopped"


def _restart(settings, instance_id: str) -> None:
    compose_file = Path(settings.greffon_path) / instance_id / "docker-compose.yml"
    subprocess.run(
        ["docker-compose", "-p", instance_id, "-f", str(compose_file), "up", "-d"],
        capture_output=True, text=True, timeout=300,
    )


def _post_callback(settings, instance_id: str, action: str, payload: dict) -> None:
    """POST the result to the manager (``X-Greffer-Token``); never raises (a lost
    callback is recovered by the manager reaper / boot reconciliation)."""
    try:
        requests.post(
            f"{settings.greffon_base_server}/api/greffer/instances/{instance_id}/{action}/",
            json=payload,
            headers={"X-Greffer-Token": settings.greffer_token or ""},
            verify=settings.greffer_ssl_verify,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("backup_callback_failed instance=%s action=%s", instance_id, action)


def backup_instance(settings, instance_id: str, backup_id: str) -> None:
    """Cold backup background job. Always restarts a running instance (try/finally)."""
    payload = {"backup_id": backup_id, "status": "failed", "error_code": "snapshot_failed"}
    was_running = compose.get_status(instance_id).get("status") == "running"
    try:
        if compose.get_status(instance_id).get("status") == "unknow":
            payload["error_code"] = "instance_missing"
            return
        if was_running:
            compose.stop({"id": instance_id})
            if not _wait_stopped(instance_id, settings.backup_stop_timeout_seconds):
                payload["error_code"] = "stop_timeout"
                return  # do NOT snapshot a non-quiescent instance
        volumes = _data_volumes(instance_id)
        rc, out, err = _run_restic(
            settings,
            ["backup", "/data", "--json", "--tag", f"instance:{instance_id}",
             "--host", settings.greffer_id],
            volumes, read_only=True,
        )
        if rc != 0:
            payload["error_code"] = _classify(err)
            return
        snapshot_id, bytes_added = _parse_summary(out)
        payload = {"backup_id": backup_id, "status": "success",
                   "snapshot_id": snapshot_id, "bytes_added": bytes_added}
    except BackupError as exc:
        payload["error_code"] = exc.code
    except Exception:  # noqa: BLE001
        logger.exception("backup_instance_failed instance=%s", instance_id)
        payload["error_code"] = "snapshot_failed"
    finally:
        if was_running:
            try:
                _restart(settings, instance_id)
            except Exception:  # noqa: BLE001
                logger.exception("backup_restart_failed instance=%s", instance_id)
        _post_callback(settings, instance_id, "backup-result", payload)


def restore_instance(settings, instance_id: str, restic_snapshot_id: str,
                     restore_id: str) -> None:
    """Restore-in-place background job: stop -> wait -> SAFETY snapshot -> restore
    volumes -> leave stopped -> callback (the manager runs the start)."""
    payload = {"restore_id": restore_id, "status": "failed", "error_code": "restore_failed"}
    started_stopped = compose.get_status(instance_id).get("status") == "running"
    safety_id = ""
    try:
        if started_stopped:
            compose.stop({"id": instance_id})
            if not _wait_stopped(instance_id, settings.backup_stop_timeout_seconds):
                payload["error_code"] = "stop_timeout"
                return
        volumes = _data_volumes(instance_id)
        # SAFETY snapshot of the now-stopped instance (the reversibility net).
        rc, out, err = _run_restic(
            settings,
            ["backup", "/data", "--json", "--tag", f"safety:{instance_id}",
             "--host", settings.greffer_id],
            volumes, read_only=True,
        )
        if rc != 0:
            payload["error_code"] = "safety_snapshot_failed"
            return  # nothing overwritten; manager re-starts via callback failure
        safety_id, _ = _parse_summary(out)
        # Overwrite the volumes from the requested snapshot.
        rc, out, err = _run_restic(
            settings,
            ["restore", restic_snapshot_id, "--target", "/", "--include", "/data",
             "--delete"],
            volumes, read_only=False,
        )
        if rc != 0:
            payload = {"restore_id": restore_id, "status": "failed",
                       "error_code": _restore_classify(err),
                       "safety_restic_snapshot_id": safety_id}
            return
        payload = {"restore_id": restore_id, "status": "success",
                   "safety_restic_snapshot_id": safety_id}
    except BackupError as exc:
        payload["error_code"] = exc.code
        payload["safety_restic_snapshot_id"] = safety_id
    except Exception:  # noqa: BLE001
        logger.exception("restore_instance_failed instance=%s", instance_id)
        payload["safety_restic_snapshot_id"] = safety_id
    finally:
        # On a PRE-overwrite failure the instance was stopped -> restart it to
        # restore service (the manager only starts on success).
        if started_stopped and payload.get("status") != "success" \
                and payload.get("error_code") in ("stop_timeout", "safety_snapshot_failed"):
            try:
                _restart(settings, instance_id)
            except Exception:  # noqa: BLE001
                logger.exception("restore_abort_restart_failed instance=%s", instance_id)
        _post_callback(settings, instance_id, "restore-result", payload)


def _restore_classify(stderr: str) -> str:
    base = _classify(stderr)
    return "restore_failed" if base == "snapshot_failed" else base


def _parse_summary(stdout: str) -> tuple[str, int | None]:
    import json
    snapshot_id, bytes_added = "", None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("message_type") == "summary":
            snapshot_id = str(obj.get("snapshot_id") or "")[:64]
            added = obj.get("data_added")
            if isinstance(added, int):
                bytes_added = added
    return snapshot_id, bytes_added


def spawn_backup(settings, instance_id: str, backup_id: str) -> None:
    """Acquire the in-process lock (non-blocking) and run the job in a thread.
    Raises BusyError (-> 409) if a concurrent op holds the lock."""
    lock = _instance_lock(instance_id)
    if not lock.acquire(blocking=False):
        raise BusyError(instance_id)
    threading.Thread(
        target=_locked_job, args=(lock, backup_instance, settings, instance_id, backup_id),
        daemon=True,
    ).start()


def spawn_restore(settings, instance_id: str, restic_snapshot_id: str,
                  restore_id: str) -> None:
    lock = _instance_lock(instance_id)
    if not lock.acquire(blocking=False):
        raise BusyError(instance_id)
    threading.Thread(
        target=_locked_job,
        args=(lock, restore_instance, settings, instance_id, restic_snapshot_id, restore_id),
        daemon=True,
    ).start()


def _locked_job(lock: threading.Lock, fn, *args) -> None:
    try:
        fn(*args)
    finally:
        lock.release()
