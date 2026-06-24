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
import shlex
import subprocess
import threading
import time
from pathlib import Path

import requests

from apps.utils.docker import compose, observe
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


def _restore_database(settings, db_container_id: str, restore_argv: list[str],
                      restic_snapshot_id: str, dump_filename: str, *,
                      timeout: int = 3600) -> None:
    """Reverse of ``_dump_and_backup``: stream ``restic dump <snap> <file>`` INTO
    ``docker exec -i <db> <restore_argv>`` (e.g. pg_restore) with DUAL exit gating.

    A failed ``restic dump`` (PRODUCER) feeds pg_restore a TRUNCATED stream, and a
    failed restore (CONSUMER) leaves a half-applied (CORRUPT) database -- so BOTH
    ends must be checked. Either non-zero -> ``restore_failed`` (the orchestrator
    then rolls back to the safety snapshot). ``restore_argv`` is shell-free (the
    catalog restore hook) and the caller wraps it in an in-container ``timeout``
    so a hung restore self-kills. Restore creds come from the DB container env via
    ``docker exec`` inheritance, never the argv (ps-safe); ``dump_filename`` is the
    path inside the snapshot that the backup wrote with ``--stdin-filename``."""
    env = restic_env(settings)
    dump_cmd = ["docker", "run", "--rm"]
    for key in env:
        if key in ("PATH", "HOME"):
            continue
        dump_cmd += ["--env", key]  # name-only; value via env= below
    dump_cmd += ["--entrypoint", "restic", settings.restic_sidecar_image,
                 "dump", restic_snapshot_id, dump_filename]
    producer = subprocess.Popen(
        dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    consumer = subprocess.Popen(
        ["docker", "exec", "-i", db_container_id, *restore_argv],
        stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    # Close OUR copy of the producer stdout so restic receives SIGPIPE if the
    # restore (consumer) dies first.
    producer.stdout.close()
    try:
        consumer.communicate(timeout=timeout)
        # A producer (restic dump) that hangs after the consumer exits must
        # become a classified 'timeout', not a bare TimeoutExpired (mirrors the
        # #112 dump path P2 fix).
        producer.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        producer.kill()
        consumer.kill()
        for p in (producer, consumer):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise BackupError("timeout")
    # Gate BOTH ends -- the orchestrator rolls back on either -- but distinguish
    # them for triage: a producer (restic dump) failure is a repo/snapshot
    # problem (truncated stream), a consumer (pg_restore) failure is a dump-
    # content or target-DB problem (half-applied). Producer checked first.
    if producer.returncode != 0:
        raise BackupError("restore_dump_failed")
    if consumer.returncode != 0:
        raise BackupError("restore_failed")


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

    FAILS LOUD on an unclassified volume (``volume_unclassified``) rather than
    silently drop it from the snapshot -- the manager must classify EVERY volume
    for a hot backup. ``regenerable`` is skipped (rebuilt on start); ``database``
    is skipped HERE because it is captured by its dump hook, not a raw snapshot
    (see ``_run_hot_backup``).

    MAY return ``[]`` -- a database-only app has no data volumes but is still
    backupable via its dump. The CALLER decides whether the overall backup has
    any artifact (data mounts OR a DB dump); this function no longer raises
    ``no_data_volumes`` on its own. l4_ports.json only rides along WITH real data,
    so it can never by itself make a dataless snapshot look successful.

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
        elif cls in ("regenerable", "database"):
            continue  # regenerable: rebuilt on start; database: dumped, not snapped
        else:
            # Unclassified: the manager must classify EVERY volume for a hot
            # backup. Refuse rather than silently exclude it.
            raise BackupError("volume_unclassified")
    if mounts:
        l4 = Path(settings.greffon_path) / instance_id / "l4_ports.json"
        if l4.exists():
            mounts.append((str(l4), "/data/_l4_ports.json"))
    return mounts


_DUMP_HOOK_LABEL = "com.greffon.backup.dump"
_DUMP_TIMEOUT_SECONDS = 3600


def _dump_hooks(instance_id: str) -> list[tuple[str, str, list[str]]]:
    """``(service, container_id, dump_argv)`` for each RUNNING container that
    declares a dump hook (the SERVICE label ``com.greffon.backup.dump`` -- service
    labels survive compose render, unlike volume labels, HLD A1).

    The hook value is a shell-free command string -> ``shlex.split`` to argv (A5:
    argv, never a shell, since the catalog is only semi-trusted). The dump runs
    UNDER an in-container ``timeout`` so a hung dump SELF-KILLS inside the DB
    container -- closing #112's gap where ``producer.kill()`` only reaped the
    local ``docker exec`` client and orphaned the in-container pg_dump.

    DB credentials are NOT in the argv: ``docker exec`` inherits the container's
    env (PGPASSWORD etc.), and the catalog bakes literal -U/-d values. Secrets
    never touch the command line (ps-safe)."""
    hooks = []
    for c in observe.list_instance_containers(instance_id):
        if getattr(c, "status", None) != "running":
            continue
        cmd = (c.labels or {}).get(_DUMP_HOOK_LABEL)
        if not cmd:
            continue
        service = (c.labels or {}).get("com.docker.compose.service") or c.name
        argv = ["timeout", str(_DUMP_TIMEOUT_SECONDS), *shlex.split(cmd)]
        hooks.append((service, c.id, argv))
    return hooks


def _run_hot_backup(settings, instance_id: str, backup_id: str,
                    volume_classes: dict) -> dict:
    """Multi-artifact HOT backup (no stop): the data-class volumes -> ONE restic
    snapshot; each database volume's dump hook -> its OWN dump snapshot. Returns
    the success payload, including a per-artifact ``manifest`` ({artifact ->
    restic_snapshot_id}) the restore reads to reassemble the instance.

    Raises BackupError on ANY failure -- never records a partial backup, because a
    missing artifact means an unrestorable instance (a backed-up filesystem with a
    lost database is worse than an obvious failure)."""
    db_volumes = [v for v, c in volume_classes.items() if c == "database"]
    mounts = _hot_backup_mounts(settings, instance_id, volume_classes)
    if not mounts and not db_volumes:
        # Nothing classified as data and no DB to dump -> an empty snapshot is not
        # a backup. Fail loud rather than record a false success.
        raise BackupError("no_data_volumes")
    # Reconcile the database VOLUMES against the dump HOOKS, and do it BEFORE the
    # data snapshot (a failure here leaves no orphan snapshot). V1 supports
    # exactly ONE database volume dumped by exactly ONE hook: any other count is
    # an ambiguous volume<->hook mapping that could SILENTLY OMIT a database with
    # a success result (e.g. two DB volumes but one hook -> one DB dumped, the
    # other skipped-from-mounts AND never dumped). Refuse rather than lose data;
    # multi-DB needs a per-volume->service mapping (a future PR).
    hooks = _dump_hooks(instance_id) if db_volumes else []
    if db_volumes:
        if len(db_volumes) > 1 or len(hooks) > 1:
            raise BackupError("multiple_database_unsupported")
        if not hooks:
            raise BackupError("no_dump_hook")
    ensure_repo(settings)
    manifest: dict[str, str] = {}
    total_bytes = 0
    if mounts:
        rc, out, err = _run_restic(
            settings,
            ["backup", "/data", "--json", "--tag", f"instance:{instance_id}",
             "--host", settings.greffer_id],
            mounts, read_only=True,
        )
        if rc != 0:
            raise BackupError(_classify(err))
        snapshot_id, bytes_added = _parse_summary(out)
        manifest["data"] = snapshot_id
        total_bytes += bytes_added or 0
    for service, container_id, dump_argv in hooks:
        snapshot_id, bytes_added = _dump_and_backup(
            settings, instance_id, container_id, dump_argv,
            f"{instance_id}/{service}.dump")
        manifest[f"db:{service}"] = snapshot_id
        total_bytes += bytes_added or 0
    # The primary snapshot_id stays the DATA snapshot (back-compat with the
    # single-snapshot manager); the manifest carries the full artifact set.
    return {"backup_id": backup_id, "status": "success",
            "snapshot_id": manifest.get("data") or next(iter(manifest.values())),
            "bytes_added": total_bytes, "manifest": manifest}


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
            # HOT: never stop. Multi-artifact -- the DATA-class volumes go to one
            # restic snapshot and each database volume's dump hook to its own
            # snapshot (the per-volume classification IS the author's consistency
            # contract, HLD A2; we never hot-back-up an unclassified app).
            # _run_hot_backup raises BackupError on any failure (no partial).
            payload = _run_hot_backup(
                settings, instance_id, backup_id, volume_classes)
        else:
            if was_running:
                # Durable marker (boot reconciliation restarts a mid-backup-
                # stopped instance if a crash skips the finally restart).
                _write_json(_backup_marker(settings, instance_id),
                            {"backup_id": backup_id})
                compose.stop({"id": instance_id})
                stopped_for_backup = True
                if not _wait_stopped(
                        instance_id, settings.backup_stop_timeout_seconds):
                    payload["error_code"] = "stop_timeout"
                    return  # do NOT snapshot a non-quiescent instance
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


_RESTORE_HOOK_LABEL = "com.greffon.backup.restore"
_DB_READY_TIMEOUT_SECONDS = 180


def _wait_db_healthy(instance_id: str, service: str, timeout: int) -> bool:
    """Poll the named DB service container until its compose healthcheck reports
    ``healthy``, bounded by ``timeout``. A database service MUST declare a
    healthcheck for hot DB restore (the catalog contract): without one we cannot
    know it is ready to accept pg_restore, so an absent / never-healthy check
    returns False and the caller fails loud rather than restore into a not-ready
    (or still-initialising) DB."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for c in observe.list_instance_containers(instance_id):
            if (c.labels or {}).get("com.docker.compose.service") != service:
                continue
            status = (c.attrs.get("State", {}).get("Health") or {}).get("Status")
            if status == "healthy":
                return True
            break  # found the service, not healthy yet -> re-poll after sleep
        time.sleep(2)
    return False


def _restore_hook_for_service(instance_id: str, service: str):
    """``(container_id, restore_argv)`` for a RUNNING service that declares a
    restore hook (the SERVICE label ``com.greffon.backup.restore``), wrapped in an
    in-container ``timeout`` (mirrors the dump hook). None if the service is not
    running or declares no hook -- the caller fails loud (a DB artifact with no way
    to restore must never silently no-op)."""
    for c in observe.list_instance_containers(instance_id):
        if (c.labels or {}).get("com.docker.compose.service") != service:
            continue
        if getattr(c, "status", None) != "running":
            return None
        cmd = (c.labels or {}).get(_RESTORE_HOOK_LABEL)
        if not cmd:
            return None
        return c.id, ["timeout", str(_DUMP_TIMEOUT_SECONDS), *shlex.split(cmd)]
    return None


def restore_instance(settings, instance_id: str, restic_snapshot_id: str,
                     restore_id: str, destination=None,
                     manifest=None, volume_classes=None) -> None:
    """Restore-in-place background job. DATA-only (cold / single-snapshot): stop ->
    wait -> SAFETY snapshot -> restore volumes -> leave stopped -> callback (the
    manager runs the start). MULTI-ARTIFACT (manifest carries ``db:<service>``
    entries, HLD A4): additionally start the instance, wait for the DB healthy, and
    ``restic dump | pg_restore`` each DB artifact, leaving the instance RUNNING
    (``already_running`` tells the manager not to re-start). On ANY failure the
    instance is left stopped with the ``safety_restic_snapshot_id`` so the operator
    can roll back. ``destination`` (Epic B) routes restic to the brokered repo."""
    settings = _effective_settings(settings, destination)
    payload = {"restore_id": restore_id, "status": "failed", "error_code": "restore_failed"}
    started_stopped = compose.get_status(instance_id).get("status") == "running"
    safety_id = ""
    overwrite_started = False
    db_started = False
    # {service: snapshot_id} for each DB dump artifact in the manifest.
    db_artifacts = {k[len("db:"):]: v for k, v in (manifest or {}).items()
                    if k.startswith("db:")}
    try:
        if started_stopped:
            compose.stop({"id": instance_id})
            if not _wait_stopped(instance_id, settings.backup_stop_timeout_seconds):
                payload["error_code"] = "stop_timeout"
                return
        ensure_repo(settings)
        if db_artifacts and not volume_classes:
            # Without the class map we can't tell data volumes from the DB volume,
            # and must NOT wipe the DB volume with --delete. Refuse.
            payload["error_code"] = "volume_unclassified"
            return
        # SAFETY snapshot of the now-stopped instance over ALL volumes (the full
        # rollback net, incl. the current DB volume).
        safety_mounts = _backup_mounts(settings, instance_id)
        rc, out, err = _run_restic(
            settings,
            ["backup", "/data", "--json", "--tag", f"safety:{instance_id}",
             "--host", settings.greffer_id],
            safety_mounts, read_only=True,
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
        # Overwrite the DATA volumes from the data snapshot. When a DB is present
        # use DATA-ONLY mounts so --delete never wipes the un-snapshotted DB volume
        # (it is repopulated by pg_restore below); else mount all (cold path).
        data_snap = (manifest or {}).get("data") or restic_snapshot_id
        data_mounts = (_hot_backup_mounts(settings, instance_id, volume_classes)
                       if db_artifacts else safety_mounts)
        if data_mounts:  # a DB-only app has no data volumes -> skip to the DB
            rc, out, err = _run_restic(
                settings,
                ["restore", data_snap, "--target", "/", "--include", "/data",
                 "--delete"],
                data_mounts, read_only=False,
            )
            if rc != 0:
                payload = {"restore_id": restore_id, "status": "failed",
                           "error_code": _restore_classify(err),
                           "safety_restic_snapshot_id": safety_id}
                return
        if db_artifacts:
            # Start the instance so the DB is live for pg_restore, wait for its
            # healthcheck, then stream each dump in. Any failure raises / returns
            # -> the finally stops the instance and leaves safety_id for rollback.
            _restart(settings, instance_id)
            db_started = True
            for service, snap in db_artifacts.items():
                if not _wait_db_healthy(
                        instance_id, service, _DB_READY_TIMEOUT_SECONDS):
                    payload = {"restore_id": restore_id, "status": "failed",
                               "error_code": "db_not_ready",
                               "safety_restic_snapshot_id": safety_id}
                    return
                hook = _restore_hook_for_service(instance_id, service)
                if hook is None:
                    payload = {"restore_id": restore_id, "status": "failed",
                               "error_code": "no_restore_hook",
                               "safety_restic_snapshot_id": safety_id}
                    return
                container_id, restore_argv = hook
                _restore_database(settings, container_id, restore_argv, snap,
                                  f"{instance_id}/{service}.dump")  # raises on fail
            # SUCCESS: the DB path leaves the instance RUNNING; the manager must
            # NOT re-start it (already_running).
            payload = {"restore_id": restore_id, "status": "success",
                       "safety_restic_snapshot_id": safety_id,
                       "already_running": True}
        else:
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
        # Restart on a PRE-overwrite failure (instance was stopped, nothing
        # touched) -> restore service. After the overwrite began, leave stopped
        # (manager starts on the success callback for the data path).
        if started_stopped and not overwrite_started \
                and payload.get("status") != "success":
            try:
                _restart(settings, instance_id)
            except Exception:  # noqa: BLE001
                logger.exception("restore_abort_restart_failed instance=%s", instance_id)
        elif db_started and payload.get("status") != "success":
            # The DB path started the instance; a failure here leaves it running a
            # corrupt/partial DB -> STOP it (operator rolls back via safety_id).
            try:
                compose.stop({"id": instance_id})
            except Exception:  # noqa: BLE001
                logger.exception("restore_db_abort_stop_failed instance=%s", instance_id)
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
                  restore_id: str, destination=None,
                  manifest=None, volume_classes=None) -> None:
    lock = _instance_lock(instance_id)
    if not lock.acquire(blocking=False):
        raise BusyError(instance_id)
    threading.Thread(
        target=_locked_job,
        args=(lock, restore_instance, settings, instance_id, restic_snapshot_id,
              restore_id, destination, manifest, volume_classes),
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
