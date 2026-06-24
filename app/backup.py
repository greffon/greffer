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

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

import requests

from apps.utils.docker import compose
from app.token import resolve_token

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


# A single process-wide lock serializing repo-WIDE ops (prune / check) so two
# triggers never spawn redundant sidecars. restic's own exclusive repo lock is the
# CROSS-PROCESS guarantee against a running backup sidecar; this only dedupes ours.
#
# PER-REPO (Epic B): prune/check of DIFFERENT (per-tenant managed/BYO) repos must
# run concurrently -- a single process-wide lock would 409 every tenant's prune
# behind one. Keyed by the restic repo URL: same repo serializes (exclusive prune),
# different repos don't contend. The guard protects the dict; the manager's sweep
# cadence/caps bound how many sidecars run at once.
_REPO_OP_LOCKS: dict[str, threading.Lock] = {}
_REPO_OP_LOCKS_GUARD = threading.Lock()


def _repo_op_lock(repo: str) -> threading.Lock:
    with _REPO_OP_LOCKS_GUARD:
        return _REPO_OP_LOCKS.setdefault(repo, threading.Lock())


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


class _BrokeredSettings:
    """Wraps the greffer settings but overrides ONLY the restic repo + S3 creds
    from a manager-brokered destination block (Epic B managed / white-glove BYO).
    Every other attribute delegates to the real settings, so the whole
    restic_env/_run_restic/ensure_repo/_forget chain targets a per-tenant
    destination with no signature churn -- backup_instance/restore_instance just
    rebind ``settings`` to this proxy when a destination is present. The block is
    in-transit only (CA-verified controller call); nothing is persisted."""

    __slots__ = ("_settings", "_dest")

    def __init__(self, settings, destination):
        self._settings = settings
        self._dest = destination

    @property
    def greffer_backup_repo(self):
        return self._dest.repo

    @property
    def restic_password(self):
        return self._dest.restic_password

    @property
    def restic_password_file(self):
        # A brokered password is inline; never fall back to the env file (which
        # would otherwise take precedence in restic_env and target the wrong repo).
        return None

    @property
    def aws_access_key_id(self):
        return self._dest.aws_access_key_id or self._settings.aws_access_key_id

    @property
    def aws_secret_access_key(self):
        return self._dest.aws_secret_access_key or self._settings.aws_secret_access_key

    def __getattr__(self, name):
        return getattr(self._settings, name)


def _effective_settings(settings, destination):
    """The settings restic should run against: the brokered destination if the
    manager supplied one, else the greffer's own env (self-managed, unchanged)."""
    if destination is None:
        return settings
    return _BrokeredSettings(settings, destination)


def _data_volumes(instance_id: str) -> list[str]:
    """The instance's DATA volumes (``<id>_*``), excluding the regenerated
    ``<id>_nginx_volume``."""
    vols = compose.client.volumes.list(filters={"name": instance_id})
    return sorted(
        v.name for v in vols
        if v.name.startswith(f"{instance_id}_") and not v.name.endswith(_NGINX_VOLUME_SUFFIX)
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


def _run_restic(settings, args: list[str], mounts: list[tuple[str, str]], *,
                read_only: bool, timeout: int = 3600) -> tuple[int, str, str]:
    """Run the digest-pinned restic sidecar with ``(source, dest)`` mounts under
    ``/data``. Secrets reach the container via ``--env KEY`` (NAME-only) +
    ``subprocess env=`` -- NEVER ``--env KEY=VALUE``, which would put the repo
    URL / password / S3 secret in the ``docker run`` ARGV (readable via
    ``ps``/``/proc/<pid>/cmdline``). Returns (rc, stdout, stderr)."""
    env = restic_env(settings)
    docker_args = ["docker", "run", "--rm"]
    for key in env:
        if key in ("PATH", "HOME"):
            continue  # for the launcher's own process, not forwarded
        docker_args += ["--env", key]  # name-only: value comes from env= below
    suffix = ":ro" if read_only else ""
    for source, dest in mounts:
        docker_args += ["-v", f"{source}:{dest}{suffix}"]
    docker_args += ["--entrypoint", "restic", settings.restic_sidecar_image, *args]
    proc = subprocess.run(
        docker_args, capture_output=True, text=True, timeout=timeout, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _dump_and_backup(settings, instance_id: str, db_container_id: str,
                     dump_argv: list[str], stdin_filename: str, *,
                     timeout: int = 3600) -> tuple[str, int]:
    """Stream ``docker exec <db> <dump_argv>`` INTO ``restic backup --stdin`` for a
    ``database`` volume's hot backup, with DUAL exit gating (HLD A3).

    The A3 trap: ``restic backup --stdin`` exits 0 on a TRUNCATED stdin (it can't
    tell the producer died mid-dump), so gating on restic alone would record a
    corrupt/partial dump as SUCCESS. We therefore gate on the PRODUCER (pg_dump)
    exit code too -- a non-zero dump fails the whole backup -- and reject a
    zero-byte dump that somehow exited 0.

    ``dump_argv`` is a LIST (shell-free; the catalog hook is semi-trusted). The
    dump's stderr is DEVNULL'd: it can leak the DB connection string/creds (like
    restic stderr), the exit code is what gates, and DEVNULL also avoids a
    stderr-pipe deadlock without a draining thread. Returns
    ``(snapshot_id, bytes_added)``; raises BackupError on failure."""
    env = restic_env(settings)
    restic_args = ["docker", "run", "--rm", "-i"]
    for key in env:
        if key in ("PATH", "HOME"):
            continue
        restic_args += ["--env", key]  # name-only; value via env= below
    restic_args += ["--entrypoint", "restic", settings.restic_sidecar_image,
                    "backup", "--stdin", "--stdin-filename", stdin_filename,
                    "--json", "--tag", f"instance:{instance_id}",
                    "--host", settings.greffer_id]
    producer = subprocess.Popen(
        ["docker", "exec", db_container_id, *dump_argv],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    consumer = subprocess.Popen(
        restic_args, stdin=producer.stdout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    # Close OUR copy of the producer stdout so the producer receives SIGPIPE if
    # the consumer (restic) dies first -- otherwise it could hang writing the dump.
    producer.stdout.close()
    try:
        out, err = consumer.communicate(timeout=timeout)
        # INSIDE the try: a producer that hangs after the consumer exits (ignores
        # SIGPIPE / pg_dump stuck in 'D') must become a classified 'timeout', not
        # a bare TimeoutExpired the caller mis-reports as snapshot_failed.
        producer.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        producer.kill()
        consumer.kill()
        # REAP both LOCAL docker clients: kill() alone leaves zombies until GC,
        # which on the single-process greffer accumulate across repeated timeouts.
        # NOTE: this kills the local `docker exec`/`docker run` CLIENTS, not the
        # in-container pg_dump -- killing a `docker exec` does not signal the
        # exec'd process, so a hung dump can ORPHAN in the DB container until it
        # finishes / the container restarts. The durable fix is wiring-time: run
        # the dump under an in-container `timeout` so it self-kills (the wiring PR
        # constructs the dump argv and owns that wrapper).
        for p in (producer, consumer):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise BackupError("timeout")
    # A3: the PRODUCER gates FIRST -- a failed/truncated/SIGPIPE'd dump fails the
    # backup even though restic --stdin exited 0 on the partial stream.
    if producer.returncode != 0:
        raise BackupError("dump_failed")
    if consumer.returncode != 0:
        raise BackupError(_classify(err))
    snapshot_id, bytes_added = _parse_summary(out)
    if not bytes_added:
        # A real pg_dump always emits a header; a zero-byte 'success' is a
        # truncated/empty dump the producer rc didn't catch -> fail loud.
        raise BackupError("dump_empty")
    return snapshot_id, bytes_added


def ensure_repo(settings) -> None:
    """First-use ``restic init`` (HLD section 4.1): a missing repo is initialized
    rather than failing every backup with ``repo_uninitialized``. Also clears a
    stale lock from a crashed prior sidecar (best-effort; the repo is per-greffer
    and this greffer is single-process)."""
    rc, _out, _err = _run_restic(settings, ["cat", "config"], [], read_only=True)
    if rc != 0:
        init_rc, _o, init_err = _run_restic(settings, ["init"], [], read_only=True)
        if init_rc != 0:
            raise BackupError(_classify(init_err))
    _run_restic(settings, ["unlock"], [], read_only=True)


def _forget(settings, instance_id: str, *, safety: bool) -> None:
    """Best-effort ``restic forget`` retention (Feature #5). Builds the keep args
    INSIDE the try so even a settings/attr error is swallowed (a forget failure
    must NEVER fail the op it follows -- the snapshot already succeeded).

    - **keep-last is FLOORED at 1** so a misconfigured NEGATIVE value can never
      delete the just-created ``safety:<id>`` snapshot before a restore overwrite.
    - **Tag-isolation:** restic ``--tag`` matches EXACTLY (not by prefix), so the
      instance policy never touches a ``safety:<id>`` snapshot and vice-versa.
    - **``--group-by tags``** pins the grouping so the keep counts can't silently
      multiply if a snapshot's host/paths ever drift.
    - **own short timeout** (it runs off the downtime-critical path, but bound it
      anyway). NOT ``--prune`` (exclusive + repo-wide, a separate cadence)."""
    try:
        if safety:
            tag = f"safety:{instance_id}"
            keep = ["--keep-last", str(max(1, settings.backup_safety_keep_last))]
        else:
            tag = f"instance:{instance_id}"
            keep = ["--keep-daily", str(max(0, settings.backup_keep_daily)),
                    "--keep-weekly", str(max(0, settings.backup_keep_weekly)),
                    "--keep-monthly", str(max(0, settings.backup_keep_monthly))]
        rc, _out, err = _run_restic(
            settings, ["forget", "--group-by", "tags", "--tag", tag, *keep],
            [], read_only=True,
            timeout=getattr(settings, "backup_forget_timeout_seconds", 300))
        if rc != 0:
            logger.warning("restic_forget_failed tag=%s code=%s",
                           tag, _classify(err))
    except Exception:  # noqa: BLE001 -- retention is best-effort, never fatal
        logger.exception("restic_forget_error instance=%s safety=%s",
                         instance_id, safety)


def _backup_mounts(settings, instance_id: str) -> list[tuple[str, str]]:
    """The data volumes (``<id>_*`` excl. nginx) mounted at ``/data/<vol>``, plus
    ``l4_ports.json`` for L4 greffons -- the one non-regenerable instance-dir file
    (HLD section 1). Everything else in the instance dir is regenerated on start."""
    mounts = [(vol, f"/data/{vol}") for vol in _data_volumes(instance_id)]
    l4 = Path(settings.greffon_path) / instance_id / "l4_ports.json"
    if l4.exists():
        mounts.append((str(l4), "/data/_l4_ports.json"))
    return mounts


def _hot_backup_mounts(settings, instance_id: str,
                       volume_classes: dict) -> list[tuple[str, str]]:
    """HOT mounts: the DATA-class volumes only. ``volume_classes`` is keyed by
    COMPOSE volume name; the actual docker volumes are ``<id>_<vol>`` -- strip
    the prefix to look up the class.

    FAILS LOUD rather than produce a partial/empty backup that would record as a
    false success (silent data loss):
    - a real data volume that is NOT classified ``data``/``regenerable`` (i.e.
      unclassified, or an unexpected ``database`` that slipped past the manager)
      raises ``volume_unclassified`` -- never silently dropped from the snapshot;
    - if NO ``data`` volume would be snapshotted, raises ``no_data_volumes`` so an
      empty / L4-only snapshot is never recorded as a good backup.

    The instance keeps running; restic captures a live (per-volume crash-
    consistent) snapshot, which the ``data`` classification declares acceptable
    for that volume."""
    prefix = f"{instance_id}_"
    mounts = []
    for vol in _data_volumes(instance_id):
        compose_name = vol[len(prefix):] if vol.startswith(prefix) else vol
        cls = volume_classes.get(compose_name)
        if cls == "data":
            mounts.append((vol, f"/data/{vol}"))
        elif cls == "regenerable":
            continue  # intentionally not backed up (rebuilt on start)
        else:
            # Unclassified or unexpected: the manager must classify EVERY volume
            # for a hot backup. Refuse rather than silently exclude it.
            raise BackupError("volume_unclassified")
    if not mounts:
        # All volumes regenerable, or class keys didn't match any real volume.
        # An L4-only snapshot is not a backup -- fail instead of false-success.
        raise BackupError("no_data_volumes")
    # l4_ports.json is added AFTER the non-empty check so it can NEVER on its own
    # make a dataless snapshot look successful.
    l4 = Path(settings.greffon_path) / instance_id / "l4_ports.json"
    if l4.exists():
        mounts.append((str(l4), "/data/_l4_ports.json"))
    return mounts


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


def _post_callback(settings, instance_id: str, action: str, payload: dict) -> bool:
    """POST the result to the manager (``X-Greffer-Token``); returns True iff the
    manager acked (2xx). Never raises -- a lost callback is recovered by the
    manager reaper / greffer boot reconciliation."""
    try:
        resp = requests.post(
            f"{settings.greffon_base_server}/api/greffer/instances/{instance_id}/{action}/",
            json=payload,
            headers={"X-Greffer-Token": resolve_token(settings)},
            verify=settings.greffer_ssl_verify,
            timeout=_HTTP_TIMEOUT,
        )
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        logger.warning("backup_callback_failed instance=%s action=%s", instance_id, action)
        return False


def _instance_dir(settings, instance_id: str) -> Path:
    return Path(settings.greffon_path) / instance_id


def _backup_marker(settings, instance_id: str) -> Path:
    return _instance_dir(settings, instance_id) / ".backup_inprogress"


def _restore_state_path(settings, instance_id: str, restore_id: str) -> Path:
    return _instance_dir(settings, instance_id) / f".restore_{restore_id}.json"


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        logger.warning("backup_state_write_failed name=%s", path.name)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def restore_status(settings, instance_id: str, restore_id: str) -> dict:
    """The DURABLE outcome of a restore, for the manager's reconciler (a stuck
    RestoreRun is never blind-failed -- its volumes may be overwritten). Returns
    the persisted payload, or ``{'status': 'unknown'}`` once acked / never-ran."""
    path = _restore_state_path(settings, instance_id, restore_id)
    if not path.exists():
        return {"status": "unknown"}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "unknown"}


def reconcile_on_boot(settings) -> None:
    """Greffer boot recovery (HLD section 7): restart instances left stopped
    mid-backup (a crash between stop and restart) and re-post un-acked restore
    callbacks so a manager-restart-lost POST doesn't strand the instance.
    Best-effort; never raises. (Orphan restic sidecars are ``--rm`` and so
    auto-clean in the normal case.)"""
    root = Path(settings.greffon_path)
    if not root.exists():
        return
    for inst_dir in root.iterdir():
        if not inst_dir.is_dir():
            continue
        instance_id = inst_dir.name
        marker = inst_dir / ".backup_inprogress"
        if marker.exists():
            try:
                if compose.get_status(instance_id).get("status") == "stopped":
                    _restart(settings, instance_id)
            except Exception:  # noqa: BLE001
                logger.exception("boot_reconcile_restart_failed instance=%s", instance_id)
            _remove(marker)
        for state_file in inst_dir.glob(".restore_*.json"):
            try:
                payload = json.loads(state_file.read_text())
            except (OSError, ValueError):
                continue
            if _post_callback(settings, instance_id, "restore-result", payload):
                _remove(state_file)


def backup_instance(settings, instance_id: str, backup_id: str,
                    destination=None, volume_classes=None) -> None:
    """Backup background job. COLD (stop -> snapshot -> start) by default; HOT (no
    stop, restic-live the data-class volumes) when ``volume_classes`` is given
    (Phase 3). A cold backup that stopped a running instance always restarts it
    (try/finally); a hot backup never stops, so never restarts. ``destination``
    (Epic B) routes restic to a manager-brokered per-tenant repo."""
    settings = _effective_settings(settings, destination)
    hot = bool(volume_classes)
    payload = {"backup_id": backup_id, "status": "failed", "error_code": "snapshot_failed"}
    was_running = compose.get_status(instance_id).get("status") == "running"
    stopped_for_backup = False
    try:
        if compose.get_status(instance_id).get("status") == "unknow":
            payload["error_code"] = "instance_missing"
            return
        if hot:
            # HOT: never stop. restic-live the DATA-class volumes (skip
            # regenerable; a 'database' volume never reaches the hot path -- the
            # manager keeps DB apps cold). The per-volume `data` classification
            # IS the author's declaration that a live snapshot is acceptable for
            # that volume (HLD A2 -- per-volume labelling is the consistency
            # contract; we never hot-snapshot an unclassified app).
            mounts = _hot_backup_mounts(settings, instance_id, volume_classes)
        elif was_running:
            # Durable marker (boot reconciliation restarts a mid-backup-stopped
            # instance if a crash skips the finally restart).
            _write_json(_backup_marker(settings, instance_id), {"backup_id": backup_id})
            compose.stop({"id": instance_id})
            stopped_for_backup = True
            if not _wait_stopped(instance_id, settings.backup_stop_timeout_seconds):
                payload["error_code"] = "stop_timeout"
                return  # do NOT snapshot a non-quiescent instance
            mounts = _backup_mounts(settings, instance_id)
        else:
            mounts = _backup_mounts(settings, instance_id)
        ensure_repo(settings)
        rc, out, err = _run_restic(
            settings,
            ["backup", "/data", "--json", "--tag", f"instance:{instance_id}",
             "--host", settings.greffer_id],
            mounts, read_only=True,
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
        # Restart ONLY if THIS backup stopped a running instance (cold path). A
        # hot backup never stops, so it must never "restart" (which would be a
        # spurious recreate of a healthy running instance).
        if stopped_for_backup:
            try:
                _restart(settings, instance_id)
            except Exception:  # noqa: BLE001
                logger.exception("backup_restart_failed instance=%s", instance_id)
        _remove(_backup_marker(settings, instance_id))
        # Retention AFTER the restart (off the downtime path), only on success: a
        # hung/slow forget must not extend the backup's stop window.
        if payload.get("status") == "success":
            _forget(settings, instance_id, safety=False)
        _post_callback(settings, instance_id, "backup-result", payload)


def restore_instance(settings, instance_id: str, restic_snapshot_id: str,
                     restore_id: str, destination=None) -> None:
    """Restore-in-place background job: stop -> wait -> SAFETY snapshot -> restore
    volumes -> leave stopped -> callback (the manager runs the start). ``destination``
    (Epic B) routes restic to the same manager-brokered per-tenant repo the backup
    was written to (the safety snapshot lands there too)."""
    settings = _effective_settings(settings, destination)
    payload = {"restore_id": restore_id, "status": "failed", "error_code": "restore_failed"}
    started_stopped = compose.get_status(instance_id).get("status") == "running"
    safety_id = ""
    overwrite_started = False
    try:
        if started_stopped:
            compose.stop({"id": instance_id})
            if not _wait_stopped(instance_id, settings.backup_stop_timeout_seconds):
                payload["error_code"] = "stop_timeout"
                return
        ensure_repo(settings)
        mounts = _backup_mounts(settings, instance_id)
        # SAFETY snapshot of the now-stopped instance (the reversibility net).
        rc, out, err = _run_restic(
            settings,
            ["backup", "/data", "--json", "--tag", f"safety:{instance_id}",
             "--host", settings.greffer_id],
            mounts, read_only=True,
        )
        if rc != 0:
            payload["error_code"] = "safety_snapshot_failed"
            return  # nothing overwritten; manager re-starts via callback failure
        safety_id, _ = _parse_summary(out)
        # Durable rollback pointer written BEFORE the destructive overwrite: a
        # crash between the overwrite and the finally must not lose safety_id
        # (boot reconcile / restore-status then still surface it).
        _write_json(
            _restore_state_path(settings, instance_id, restore_id),
            {"restore_id": restore_id, "status": "overwriting",
             "safety_restic_snapshot_id": safety_id})
        overwrite_started = True
        # Overwrite the volumes from the requested snapshot.
        rc, out, err = _run_restic(
            settings,
            ["restore", restic_snapshot_id, "--target", "/", "--include", "/data",
             "--delete"],
            mounts, read_only=False,
        )
        if rc != 0:
            payload = {"restore_id": restore_id, "status": "failed",
                       "error_code": _restore_classify(err),
                       "safety_restic_snapshot_id": safety_id}
            return
        payload = {"restore_id": restore_id, "status": "success",
                   "safety_restic_snapshot_id": safety_id}
        # Bound OLD safety snapshots now the restore SUCCEEDED -- off the
        # pre-overwrite critical path; keep-last>=1 keeps this restore's new one,
        # and skipping it on a failed overwrite preserves every rollback point.
        _forget(settings, instance_id, safety=True)
    except BackupError as exc:
        payload["error_code"] = exc.code
        payload["safety_restic_snapshot_id"] = safety_id
    except Exception:  # noqa: BLE001
        logger.exception("restore_instance_failed instance=%s", instance_id)
        payload["safety_restic_snapshot_id"] = safety_id
    finally:
        # On a PRE-overwrite failure the instance was stopped -> restart it to
        # restore service (the manager only starts on success).
        # Restart only if the instance was stopped AND nothing was overwritten
        # (a pre-overwrite failure of ANY kind -- rc!=0 OR an exception/timeout)
        # -> restore service. Once the overwrite began, leave it stopped (the
        # manager runs the start on the success callback).
        if started_stopped and not overwrite_started \
                and payload.get("status") != "success":
            try:
                _restart(settings, instance_id)
            except Exception:  # noqa: BLE001
                logger.exception("restore_abort_restart_failed instance=%s", instance_id)
        # Durable restore-state, kept until the manager acks (boot reconciliation
        # re-posts a lost callback so an overwritten instance is never stranded).
        state_path = _restore_state_path(settings, instance_id, restore_id)
        _write_json(state_path, payload)
        if _post_callback(settings, instance_id, "restore-result", payload):
            _remove(state_path)


def _restore_classify(stderr: str) -> str:
    base = _classify(stderr)
    return "restore_failed" if base == "snapshot_failed" else base


def _parse_summary(stdout: str) -> tuple[str, int | None]:
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


def spawn_backup(settings, instance_id: str, backup_id: str,
                 destination=None, volume_classes=None) -> None:
    """Acquire the in-process lock (non-blocking) and run the job in a thread.
    Raises BusyError (-> 409) if a concurrent op holds the lock. ``destination``
    (Epic B) is forwarded to the brokered per-tenant repo, else None (self-managed).
    ``volume_classes`` (Phase 3) present => HOT backup, else COLD."""
    lock = _instance_lock(instance_id)
    if not lock.acquire(blocking=False):
        raise BusyError(instance_id)
    threading.Thread(
        target=_locked_job,
        args=(lock, backup_instance, settings, instance_id, backup_id,
              destination, volume_classes),
        daemon=True,
    ).start()


def spawn_restore(settings, instance_id: str, restic_snapshot_id: str,
                  restore_id: str, destination=None) -> None:
    lock = _instance_lock(instance_id)
    if not lock.acquire(blocking=False):
        raise BusyError(instance_id)
    threading.Thread(
        target=_locked_job,
        args=(lock, restore_instance, settings, instance_id, restic_snapshot_id,
              restore_id, destination),
        daemon=True,
    ).start()


def _locked_job(lock: threading.Lock, fn, *args) -> None:
    try:
        fn(*args)
    finally:
        lock.release()


def _repo_op_error_code(stderr: str) -> str:
    """A concurrent backup sidecar holds restic's exclusive repo lock, so a
    prune/check can report the repo locked -- a clean retry-next-cadence, not a
    hard failure. Match the LOCK-CONFLICT phrasing specifically, NOT a bare
    'locked' substring -- a future object-lock / governance-mode write rejection
    (managed-tier B2 Object Lock) could carry 'locked' and must NOT be swallowed
    as a benign retry."""
    s = (stderr or '').lower()
    if 'already locked' in s or 'unable to create lock' in s:
        return 'repo_busy'
    return _classify(stderr)


def prune_repo(settings) -> dict:
    """Repo-wide ``restic prune`` -- the SPACE half of retention (per-instance
    ``forget`` drops snapshot references after each backup; prune reclaims the data
    no snapshot references). EXCLUSIVE + repo-wide, hence a SEPARATE cadence from
    backup. Best-effort, detached (no callback): the manager triggers it and reads
    nothing back; a repo-busy result simply retries next cadence.

    Per-tenant (Epic B): ``settings`` may be the effective (brokered) settings for
    a managed/BYO destination, so prune runs against the per-tenant repo. The
    manager drives one prune/check per destination; the per-repo lock keeps tenants
    from contending. (Closes the prior known gap where prune/check only ever
    touched the greffer's env repo.)"""
    try:
        ensure_repo(settings)
        rc, _out, err = _run_restic(
            settings, ['prune'], [], read_only=True,
            timeout=getattr(settings, 'backup_prune_timeout_seconds', 7200))
        if rc != 0:
            code = _repo_op_error_code(err)
            logger.warning('restic_prune_failed code=%s', code)
            return {'status': 'failed', 'error_code': code}
        return {'status': 'success'}
    except Exception:  # noqa: BLE001 -- prune is best-effort, never fatal
        logger.exception('prune_repo_failed')
        return {'status': 'failed', 'error_code': 'prune_failed'}


def check_repo(settings) -> dict:
    """Periodic ``restic check`` -- repo integrity verification (epic R27).
    Detached, best-effort; read-only so it can run alongside a backup."""
    try:
        ensure_repo(settings)
        rc, _out, err = _run_restic(
            settings, ['check'], [], read_only=True,
            timeout=getattr(settings, 'backup_check_timeout_seconds', 7200))
        if rc != 0:
            code = _repo_op_error_code(err)
            logger.warning('restic_check_failed code=%s', code)
            return {'status': 'failed', 'error_code': code}
        return {'status': 'success'}
    except Exception:  # noqa: BLE001
        logger.exception('check_repo_failed')
        return {'status': 'failed', 'error_code': 'check_failed'}


def spawn_repo_op(settings, op: str, destination=None) -> None:
    """Run a repo-wide op (``prune`` | ``check``) in a background thread under a
    non-blocking PER-REPO lock. ``destination`` (Epic B) targets a manager-brokered
    per-tenant repo; None = the greffer's own env repo. Raises BusyError (-> 409) if
    that repo already has an op running, so the manager retries next cadence rather
    than stacking redundant sidecars on the same repo (a different tenant's repo is
    unaffected)."""
    eff = _effective_settings(settings, destination)
    repo = eff.greffer_backup_repo
    if not repo:
        raise BackupError('repo_uninitialized')
    lock = _repo_op_lock(repo)
    if not lock.acquire(blocking=False):
        raise BusyError(op)
    fn = prune_repo if op == 'prune' else check_repo
    try:
        threading.Thread(
            target=_repo_op_job, args=(repo, lock, fn, eff), daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 -- if the thread can't start (e.g. thread
        _reap_repo_op_lock(repo, lock)  # exhaustion), never leak this repo's lock
        raise                           # (it would 409 every future op on it).


def _reap_repo_op_lock(repo: str, lock: threading.Lock) -> None:
    """Release the per-repo lock AND drop it from the registry so the dict can't
    grow one entry per tenant repo forever. Safe because repo-op acquire is always
    NON-BLOCKING (no waiters): a concurrent spawn either still sees this lock and
    409s (we hold it until the pop), or setdefault's a FRESH lock after the pop.
    Pop under the guard, while still holding the lock, then release."""
    with _REPO_OP_LOCKS_GUARD:
        if _REPO_OP_LOCKS.get(repo) is lock:
            _REPO_OP_LOCKS.pop(repo, None)
    lock.release()


def _repo_op_job(repo: str, lock: threading.Lock, fn, settings) -> None:
    try:
        fn(settings)
    finally:
        _reap_repo_op_lock(repo, lock)
