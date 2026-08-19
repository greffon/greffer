from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from app import backup


def _settings(**kw):
    base = dict(
        greffer_id="g1", greffer_token="tok", greffon_path="/tmp",
        greffon_base_server="https://m", greffer_ssl_verify=False,
        greffer_backup_repo="s3:https://h/repo", restic_password="pw",
        restic_password_file=None, aws_access_key_id=None,
        aws_secret_access_key=None, restic_sidecar_image="restic/restic:0.17.3",
        backup_stop_timeout_seconds=5,
        backup_keep_daily=7, backup_keep_weekly=4, backup_keep_monthly=6,
        backup_safety_keep_last=3,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---- pure helpers ----------------------------------------------------------

def test_restic_env_requires_repo():
    with pytest.raises(backup.BackupError) as exc:
        backup.restic_env(_settings(greffer_backup_repo=None))
    assert exc.value.code == "repo_uninitialized"


def test_restic_env_builds_with_creds():
    env = backup.restic_env(
        _settings(aws_access_key_id="k", aws_secret_access_key="s"))
    assert env["RESTIC_REPOSITORY"] == "s3:https://h/repo"
    assert env["RESTIC_PASSWORD"] == "pw"
    assert env["AWS_ACCESS_KEY_ID"] == "k"


def test_classify():
    assert backup._classify("Fatal: wrong password") == "auth_failed"
    assert backup._classify("no space left on device") == "disk_full"
    assert backup._classify("connection refused") == "repo_unreachable"
    assert backup._classify("something weird") == "snapshot_failed"
    assert backup._restore_classify("something weird") == "restore_failed"


def test_parse_summary():
    out = ('{"message_type":"status"}\n'
           '{"message_type":"summary","snapshot_id":"abc","data_added":42}')
    assert backup._parse_summary(out) == ("abc", 42)


# ---- backup orchestration --------------------------------------------------

def _patch_common(monkeypatch, status="running", wait=True, volumes=("i_db",)):
    monkeypatch.setattr(backup.compose, "get_status",
                        lambda _id: {"status": status})
    monkeypatch.setattr(backup.compose, "stop", mock.Mock())
    monkeypatch.setattr(backup, "_wait_stopped", lambda *a: wait)
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: list(volumes))
    # ensure_repo (init/unlock) is separately tested; no-op it here so it does
    # not pollute the mocked _run_restic call sequences.
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)


def test_backup_happy_restarts_and_reports_success(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', ""))
    restart = mock.Mock()
    monkeypatch.setattr(backup, "_restart", restart)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1")

    restart.assert_called_once()
    payload = cb.call_args.args[3]
    assert cb.call_args.args[2] == "backup-result"
    assert payload["status"] == "success"
    assert payload["snapshot_id"] == "S"
    assert payload["bytes_added"] == 7
    assert payload["backup_id"] == "b1"


def _managed_dest():
    return SimpleNamespace(repo="s3:https://b2/bucket/t1", restic_password="pw",
                           aws_access_key_id="k", aws_secret_access_key="s")


def test_cold_backup_collects_repo_stats_after_restart(monkeypatch):
    # GB-metering: for a MANAGED (brokered) backup the repo-size estimate is collected OFF the
    # downtime path -- AFTER the instance restarts, never while it's still stopped (a slow restic
    # stats must not extend the cold backup's outage). Also asserts repo_bytes reaches the payload.
    _patch_common(monkeypatch)
    order = []
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', ""))
    monkeypatch.setattr(backup, "_restart", lambda *a, **k: order.append("restart"))
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)

    def _stats(*a, **k):
        order.append("stats")
        return 4096

    monkeypatch.setattr(backup, "_repo_stats", _stats)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1", destination=_managed_dest())

    assert order == ["restart", "stats"]            # stats AFTER restart -> off the downtime path
    assert cb.call_args.args[3]["repo_bytes"] == 4096


def test_self_host_backup_skips_repo_stats(monkeypatch):
    # Self-host (no brokered destination) must NOT run restic stats: repo_bytes is managed-only,
    # and the scan would hold the per-instance lock (instance_busy risk) for nothing.
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', ""))
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    stats = mock.Mock()
    monkeypatch.setattr(backup, "_repo_stats", stats)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1")  # no destination -> self-host

    stats.assert_not_called()
    assert "repo_bytes" not in cb.call_args.args[3]


def test_restic_stats_total_size_tolerates_leading_progress_line():
    # restic 0.19.x can prefix a progress line before the JSON object (restic/restic#21891);
    # the parser must still find total_size, not choke on non-JSON stdout.
    assert backup._restic_stats_total_size('{"total_size": 4096}') == 4096
    assert backup._restic_stats_total_size(
        '{"message_type":"status","percent_done":1}\n'
        '{"total_size": 8192, "total_file_count": 3}') == 8192
    assert backup._restic_stats_total_size("not json at all\n") is None
    assert backup._restic_stats_total_size('{"no_size": 1}') is None


def test_backup_stop_timeout_never_snapshots(monkeypatch):
    _patch_common(monkeypatch, wait=False)
    run = mock.Mock()
    monkeypatch.setattr(backup, "_run_restic", run)
    restart = mock.Mock()
    monkeypatch.setattr(backup, "_restart", restart)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1")

    run.assert_not_called()           # never snapshots a non-quiescent instance
    restart.assert_called_once()      # but always restarts
    assert cb.call_args.args[3]["error_code"] == "stop_timeout"


def test_backup_restic_failure_classified(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(backup, "_run_restic",
                        lambda *a, **k: (1, "", "Fatal: wrong password"))
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1")
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "auth_failed"


# ---- restore orchestration -------------------------------------------------

def test_restore_takes_safety_before_overwrite(monkeypatch):
    _patch_common(monkeypatch)
    order = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        order.append(args[0])
        if args[0] == "backup":   # the safety snapshot
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (0, "", "")        # the restore

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.restore_instance(_settings(), "i", "snap-1", "r1")

    # safety snapshot FIRST, then the overwrite, then retention (off the
    # pre-overwrite critical path)
    assert order == ["backup", "restore", "forget"]
    payload = cb.call_args.args[3]
    assert cb.call_args.args[2] == "restore-result"
    assert payload["status"] == "success"
    assert payload["safety_restic_snapshot_id"] == "SAFE"


def test_restore_safety_failure_aborts_and_restarts(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(backup, "_run_restic",
                        lambda *a, **k: (1, "", "boom"))  # safety fails
    restart = mock.Mock()
    monkeypatch.setattr(backup, "_restart", restart)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.restore_instance(_settings(), "i", "snap-1", "r1")

    restart.assert_called_once()     # nothing overwritten -> restore service
    assert cb.call_args.args[3]["error_code"] == "safety_snapshot_failed"


def test_restore_overwrite_failure_keeps_safety_pointer(monkeypatch):
    _patch_common(monkeypatch)

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (1, "", "disk full")   # the restore overwrite fails

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.restore_instance(_settings(), "i", "snap-1", "r1")
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "disk_full"
    assert payload["safety_restic_snapshot_id"] == "SAFE"   # rollback survives


# ---- locking ---------------------------------------------------------------

def _lock_is_free(instance_id: str) -> bool:
    """Whether the per-instance lock could be taken right now -- i.e. whether a
    manager control op arriving at this instant is served or refused 409
    instance_busy."""
    lock = backup._instance_lock(instance_id)
    if lock.acquire(blocking=False):
        lock.release()
        return True
    return False


# Captured at import, BEFORE any test can patch it. A second _drain inside one
# test used to capture the first drain's wrapper, nesting them -- so a job that
# raised landed in the FIRST drain's failures list, which had already returned
# unchecked, and the re-raise below silently did nothing.
_REAL_LOCKED_JOB = backup._locked_job


def _drain(monkeypatch, spawn, instance_id, *args, **kwargs):
    """Run ``spawn`` and block until its background job has finished, re-raising
    anything that job raised.

    The re-raise is load-bearing. Without it a job thread that dies -- on a double
    release, say -- still passes: the exception goes to ``threading.excepthook``,
    pytest downgrades it to a warning, and CI runs a bare ``pytest -q``. A
    regression that raised on every backup and restore would ship green.
    """
    done = threading.Event()
    failures = []

    def _job(release, fn, *a, **kw):
        try:
            _REAL_LOCKED_JOB(release, fn, *a, **kw)
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            failures.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(backup, "_locked_job", _job)
    spawn(_settings(), instance_id, *args, **kwargs)
    assert done.wait(20), "background job hung"
    if failures:
        raise AssertionError(
            f"the background job raised: {failures[0]!r}") from failures[0]


def _observe_lock(monkeypatch, instance_id, *, safety_fails=False):
    """Record the lock state at every point the job passes through, plus the
    payload it finally sends.

    Two properties, not one. The lock must be HELD while the job touches compose
    and restic -- releasing early would let a start (or a renewal tick) run
    compose against volumes mid-overwrite -- and FREE by the callback, because the
    manager answers that callback by calling straight back into /start/.

    Patches over ``_patch_common``, so call it after.
    """
    seen = {}

    def _mark(name):
        seen[name] = _lock_is_free(instance_id)

    def _cb(settings, iid, action, payload):
        _mark(action)
        seen.setdefault("payloads", {})[action] = payload
        return True

    def _stop(_spec):
        _mark("compose.stop")
        return mock.Mock()

    def _restic(settings, args, mounts, **kw):
        _mark(f"restic:{args[0]}")
        if args[0] == "backup":
            if safety_fails:
                return (1, "", "no space left on device")
            return (0, '{"message_type":"summary","snapshot_id":"SAFE",'
                       '"data_added":7}', "")
        return (0, "", "")

    def _restart(*_a, **_k):
        _mark("restart")

    monkeypatch.setattr(backup, "_post_callback", _cb)
    monkeypatch.setattr(backup.compose, "stop", _stop)
    monkeypatch.setattr(backup, "_run_restic", _restic)
    monkeypatch.setattr(backup, "_restart", _restart)
    def _write_json(_path, _data):
        _mark("write_json")

    def _forget(*_a, **_k):
        _mark("forget")

    monkeypatch.setattr(backup, "_forget", _forget)
    monkeypatch.setattr(backup, "_write_json", _write_json)
    monkeypatch.setattr(backup, "_remove", mock.Mock())
    return seen


def test_spawn_backup_busy_raises(monkeypatch):
    lock = backup._instance_lock("busy-i")
    lock.acquire()
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_backup(_settings(), "busy-i", "b1")
    finally:
        lock.release()


def test_spawn_restore_busy_raises(monkeypatch):
    """The 409 that stops a restore landing on an instance already mid-op. The
    backup side had this; the restore side never did."""
    lock = backup._instance_lock("busy-r")
    lock.acquire()
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_restore(_settings(), "busy-r", "snap-1", "r1")
    finally:
        lock.release()


def test_restore_holds_the_lock_through_its_work_then_frees_the_callback(monkeypatch):
    """The manager answers a successful restore-result INLINE by POSTing back to
    /api/controller/start/, which takes this same per-instance lock. Holding the
    lock across the callback made that start a guaranteed 409 instance_busy, so
    every data-path restore landed as ``restored_start_failed`` with the instance
    left stopped and the data perfectly fine."""
    _patch_common(monkeypatch)
    seen = _observe_lock(monkeypatch, "rel-restore")

    _drain(monkeypatch, backup.spawn_restore, "rel-restore", "snap-1", "r1")

    assert seen["compose.stop"] is False, "the stop ran with the lock free"
    assert seen["restic:backup"] is False, "the safety snapshot ran unlocked"
    assert seen["restic:restore"] is False, "the --delete overwrite ran unlocked"
    assert seen["forget"] is False, (
        "retention ran with the lock free -- _forget is unbounded and takes "
        "restic's exclusive repo lock")
    assert seen["write_json"] is False, (
        "the durable restore-state file was written after the release -- it must "
        "exist before anyone can act on the callback, or a crash loses the "
        "safety-snapshot rollback pointer")
    assert seen["restore-result"] is True, (
        "the restore job still held the per-instance lock while calling back -- "
        "the manager's inline start would be refused 409 instance_busy")
    assert seen["payloads"]["restore-result"]["status"] == "success"


def test_failed_restore_holds_the_lock_through_its_abort_restart(monkeypatch):
    """A pre-overwrite failure restarts the instance from the finally to restore
    service. That restart is compose work and must run under the lock; only the
    callback after it may run free."""
    _patch_common(monkeypatch)
    seen = _observe_lock(monkeypatch, "rel-abort", safety_fails=True)

    _drain(monkeypatch, backup.spawn_restore, "rel-abort", "snap-1", "r1")

    assert seen["restart"] is False, "the abort restart ran with the lock free"
    assert seen["restore-result"] is True
    assert seen["payloads"]["restore-result"]["status"] == "failed"


def test_backup_holds_the_lock_through_its_restart_then_frees_the_callback(monkeypatch):
    """Same rule on the backup side. The manager does not start an instance off a
    backup-result, but the migration cutover polls the snapshot to terminal --
    which this callback is what makes it -- and then acts on the instance."""
    _patch_common(monkeypatch)
    seen = _observe_lock(monkeypatch, "rel-backup")

    _drain(monkeypatch, backup.spawn_backup, "rel-backup", "b1")

    assert seen["compose.stop"] is False, "the stop ran with the lock free"
    assert seen["restart"] is False, "the cold-path restart ran with the lock free"
    assert seen["forget"] is False, (
        "retention ran with the lock free -- _forget is unbounded and takes "
        "restic's exclusive repo lock")
    assert seen["backup-result"] is True
    assert seen["payloads"]["backup-result"]["status"] == "success"


def test_hot_backup_frees_the_lock_for_its_callback(monkeypatch):
    """HOT never stops or restarts, so it exercises a different route to the same
    callback."""
    _patch_common(monkeypatch, status="running", volumes=("rel-hot_db",))
    seen = _observe_lock(monkeypatch, "rel-hot")

    _drain(monkeypatch, backup.spawn_backup, "rel-hot", "b1",
           volume_classes={"db": "data"})

    assert seen["restic:backup"] is False, "the hot snapshot ran unlocked"
    assert "restart" not in seen, "HOT must never restart"
    assert seen["backup-result"] is True
    assert seen["payloads"]["backup-result"]["status"] == "success"


def test_db_restore_frees_the_lock_for_its_callback(monkeypatch):
    """The multi-artifact path leaves the instance RUNNING and reports
    ``already_running``, so the manager skips the start -- but the lock must still
    be free, because the manager is free to act on the instance either way."""
    _patch_common(monkeypatch, volumes=("rel-db_files", "rel-db_db"))
    seen = _observe_lock(monkeypatch, "rel-db")
    monkeypatch.setattr(backup, "_start_services", mock.Mock())
    monkeypatch.setattr(backup, "_wait_db_healthy", lambda *a: True)
    monkeypatch.setattr(backup, "_restore_database", mock.Mock())
    container = mock.Mock(id="pgc", status="running")
    container.labels = {"com.docker.compose.service": "postgres",
                        "com.greffon.backup.restore": "pg_restore -d app"}
    container.attrs = {"State": {"Health": {"Status": "healthy"}},
                       "Mounts": [{"Type": "volume", "Name": "rel-db_db"}]}
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: [container])

    _drain(monkeypatch, backup.spawn_restore, "rel-db", "snap-1", "r1",
           manifest={"data": "DATA", "db:postgres": "DUMP"},
           volume_classes={"files": "data", "db": "database"})

    assert seen["restart"] is False, "the final full restart ran unlocked"
    assert seen["restore-result"] is True
    payload = seen["payloads"]["restore-result"]
    assert payload["status"] == "success"
    assert payload["already_running"] is True


def test_the_backup_and_restore_stops_are_not_inflight_tracked(monkeypatch):
    """Deliberate, and the reason is easy to lose.

    ``compose_inflight``'s only reader is the cert-renewal worker, and renewal is
    ALREADY stood off through the whole of these jobs by the per-instance lock
    (the tests above assert it is held at ``compose.stop``). So tracking buys
    nothing here -- and it costs: ``track_compose_child`` waits on the child with
    an unbounded ``proc.wait()``, so a hung docker daemon pins the counter
    forever, permanently skipping renewal for this instance (cert expiry -> 502)
    and collapsing the node's tick to the 120s deferred cadence. Backups run on a
    manager cadence, so that would be a per-cycle exposure.

    The DB-abort branch DOES track its stop, and should: it releases the lock
    immediately afterwards, so the lock is not covering that child.
    """
    import app.routers.controller as controller

    _patch_common(monkeypatch)
    _observe_lock(monkeypatch, "trk")
    tracked = []
    monkeypatch.setattr(controller, "track_compose_child",
                        lambda iid, proc: tracked.append(iid))

    _drain(monkeypatch, backup.spawn_restore, "trk", "snap-1", "r1")
    assert tracked == [], "the restore's stop child must not be inflight-tracked"

    _drain(monkeypatch, backup.spawn_backup, "trk", "b1")
    assert tracked == [], "the cold backup's stop child must not be inflight-tracked"


def test_a_thread_that_never_starts_does_not_strand_the_lock(monkeypatch):
    """The lock is acquired BEFORE the thread exists. A ``start()`` that raises
    used to leave it held forever -- every later start / stop / backup / restore
    on that instance answering 409 instance_busy until the greffer restarted,
    because the owner that would have released it never existed."""
    class _DeadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    # Swap only the module reference backup.py holds, not the real threading
    # module -- Lock still has to work, and other tests still need real threads.
    monkeypatch.setattr(
        backup, "threading",
        SimpleNamespace(Thread=_DeadThread, Lock=threading.Lock))

    # BusyError (-> 409), not the raw error (-> 500). The job provably never
    # began, so a retry is safe, and 409 is the only class the manager rolls its
    # ledger row back on -- a 500 left the RestoreRun RUNNING forever, and there
    # is no restore reaper.
    with pytest.raises(backup.BusyError):
        backup.spawn_backup(_settings(), "dead-b", "b1")
    assert _lock_is_free("dead-b"), "spawn_backup stranded the lock"

    with pytest.raises(backup.BusyError):
        backup.spawn_restore(_settings(), "dead-r", "snap-1", "r1")
    assert _lock_is_free("dead-r"), "spawn_restore stranded the lock"


def test_a_backup_that_times_out_stopping_still_frees_the_lock(monkeypatch):
    """The release must not be gated on success. A failed backup that kept the
    lock across its callback holds it for a manager round trip -- sub-second
    normally, a full 30s when the manager is unreachable, which is exactly when
    something else wants the instance."""
    _patch_common(monkeypatch, wait=False)          # the stop never quiesces
    seen = _observe_lock(monkeypatch, "to-backup")

    _drain(monkeypatch, backup.spawn_backup, "to-backup", "b1")

    assert seen["compose.stop"] is False, "the stop ran with the lock free"
    assert seen["backup-result"] is True
    assert seen["payloads"]["backup-result"]["error_code"] == "stop_timeout"


def test_a_restore_that_times_out_stopping_still_frees_the_lock(monkeypatch):
    """Same on the restore side -- and here it is worse, because the manager
    answers the callback inline."""
    _patch_common(monkeypatch, wait=False)
    seen = _observe_lock(monkeypatch, "to-restore")

    _drain(monkeypatch, backup.spawn_restore, "to-restore", "snap-1", "r1")

    assert seen["restart"] is False, "the abort restart ran with the lock free"
    assert seen["restore-result"] is True
    assert seen["payloads"]["restore-result"]["error_code"] == "stop_timeout"


def test_a_failed_db_restore_holds_the_lock_through_its_abort_teardown(monkeypatch):
    """The multi-artifact abort branch STARTED the instance for pg_restore, so its
    teardown is real compose work on a live instance -- and it is the one branch
    whose stop is genuinely fire-and-forget. The job must hold the lock across it
    AND wait for it, or a /start/ admitted the instant the lock frees serves the
    half-restored database this branch exists to take down. It stays
    inflight-tracked too, unlike its two siblings, because renewal is not covered
    by the lock once we release."""
    import app.routers.controller as controller

    _patch_common(monkeypatch, volumes=("dbab_files", "dbab_db"))
    seen = _observe_lock(monkeypatch, "dbab")
    tracked = []
    monkeypatch.setattr(controller, "track_compose_child",
                        lambda iid, proc: tracked.append(iid))
    monkeypatch.setattr(backup, "_start_services", mock.Mock())
    monkeypatch.setattr(backup, "_wait_db_healthy", lambda *a: False)  # db_not_ready
    waits = []
    monkeypatch.setattr(backup, "_wait_stopped",
                        lambda iid, timeout: (waits.append(iid), True)[1])
    container = mock.Mock(id="pgc", status="running")
    container.labels = {"com.docker.compose.service": "postgres",
                        "com.greffon.backup.restore": "pg_restore -d app"}
    container.attrs = {"State": {"Health": {"Status": "starting"}},
                       "Mounts": [{"Type": "volume", "Name": "dbab_db"}]}
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: [container])

    _drain(monkeypatch, backup.spawn_restore, "dbab", "snap-1", "r1",
           manifest={"data": "DATA", "db:postgres": "DUMP"},
           volume_classes={"files": "data", "db": "database"})

    assert seen["compose.stop"] is False, "the abort teardown ran unlocked"
    assert len(waits) == 2, (
        "the abort teardown was not waited for -- one wait is the entry stop; "
        "without the second, the lock frees while the teardown is still running "
        "and a /start/ can serve the half-restored database")
    assert tracked == ["dbab"], "the abort stop must stay inflight-tracked"
    assert seen["restore-result"] is True
    assert seen["payloads"]["restore-result"]["error_code"] == "db_not_ready"


def test_a_late_second_release_cannot_steal_a_later_ops_lock(monkeypatch):
    """The one-shot has to be a genuine no-op the second time, NOT a release that
    swallows the RuntimeError. The two are indistinguishable until something else
    holds the lock -- and by design something else does.

    The sequence is reachable: the job releases early and posts its callback; the
    manager answers it by calling /start/, which takes this lock; the greffer's
    callback POST then hits its 30s timeout (shorter than the manager's 60s
    actuation budget), the job returns, and ``_locked_job``'s finally fires the
    SECOND release while that start is still holding the lock. A swallowing
    release would free the start's lock -- a third op then runs against a live
    ``up -d``, and the start's own release raises RuntimeError -> 500 ->
    ``restored_start_failed``, the exact outcome this change exists to remove.
    """
    lock = backup._instance_lock("steal")
    assert lock.acquire(blocking=False)
    release = backup._release_once(lock)
    release()                                    # the job's early release

    assert lock.acquire(blocking=False)          # a LATER op takes it
    try:
        release()                                # _locked_job's finally, late
        assert lock.locked(), (
            "the second release freed a lock this job no longer owns -- it stole "
            "the later op's lock")
    finally:
        lock.release()


def test_lock_is_released_once_when_the_job_raises(monkeypatch):
    """The early release is one-shot: a job that drops the lock and THEN raises
    must not have ``_locked_job`` release it a second time (RuntimeError on an
    unlocked lock), and a job that never reaches the callback must still get the
    lock released by the wrapper."""
    lock = backup._instance_lock("once-i")
    assert lock.acquire(blocking=False)
    release = backup._release_once(lock)
    release()
    release()                                   # no-op, not RuntimeError
    assert lock.acquire(blocking=False)         # genuinely free, released once
    lock.release()

    # A job that raises before any early release: the wrapper still frees it.
    lock2 = backup._instance_lock("once-j")
    assert lock2.acquire(blocking=False)
    with pytest.raises(ValueError):
        backup._locked_job(backup._release_once(lock2),
                           mock.Mock(side_effect=ValueError("boom")))
    assert lock2.acquire(blocking=False)
    lock2.release()


# ---- callback ack + crash recovery ----------------------------------------

def test_post_callback_returns_ack(monkeypatch):
    s = _settings()
    monkeypatch.setattr(backup.requests, "post",
                        lambda *a, **k: mock.Mock(status_code=200))
    assert backup._post_callback(s, "i", "backup-result", {}) is True
    monkeypatch.setattr(backup.requests, "post",
                        lambda *a, **k: mock.Mock(status_code=500))
    assert backup._post_callback(s, "i", "backup-result", {}) is False

    def _raise(*a, **k):
        raise backup.requests.ConnectionError()
    monkeypatch.setattr(backup.requests, "post", _raise)
    assert backup._post_callback(s, "i", "backup-result", {}) is False


def test_restore_status_reads_durable_state(tmp_path):
    s = _settings(greffon_path=str(tmp_path))
    inst = tmp_path / "i"
    inst.mkdir()
    (inst / ".restore_r1.json").write_text(
        '{"status": "success", "safety_restic_snapshot_id": "SAFE"}')
    out = backup.restore_status(s, "i", "r1")
    assert out["status"] == "success"
    assert out["safety_restic_snapshot_id"] == "SAFE"
    assert backup.restore_status(s, "i", "missing")["status"] == "unknown"


def test_reconcile_restarts_mid_backup_stopped(tmp_path, monkeypatch):
    s = _settings(greffon_path=str(tmp_path))
    inst = tmp_path / "i"
    inst.mkdir()
    (inst / ".backup_inprogress").write_text('{"backup_id": "b1"}')
    monkeypatch.setattr(backup.compose, "get_status", lambda _id: {"status": "stopped"})
    restart = mock.Mock()
    monkeypatch.setattr(backup, "_restart", restart)
    backup.reconcile_on_boot(s)
    restart.assert_called_once()
    assert not (inst / ".backup_inprogress").exists()  # marker cleared


def test_run_restic_no_secret_in_argv(monkeypatch):
    captured = {}

    def _run(argv, **kw):
        captured["argv"], captured["env"] = argv, kw.get("env", {})
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup.subprocess, "run", _run)
    s = _settings(restic_password="SEKRET", aws_secret_access_key="AWSSEKRET")
    backup._run_restic(s, ["backup", "/data"], [("v", "/data/v")], read_only=True)
    joined = " ".join(captured["argv"])
    assert "SEKRET" not in joined and "AWSSEKRET" not in joined   # NOT in argv
    assert captured["env"]["RESTIC_PASSWORD"] == "SEKRET"          # in env
    assert "--env" in captured["argv"] and "RESTIC_PASSWORD" in captured["argv"]


def test_ensure_repo_inits_when_missing(monkeypatch):
    calls = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        calls.append(args[0])
        if args[0] == "cat":
            return (1, "", "unable to open config")  # repo missing
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    backup.ensure_repo(_settings())
    assert "cat" in calls and "init" in calls   # init'd the missing repo


def test_reconcile_reposts_lost_restore_callback(tmp_path, monkeypatch):
    s = _settings(greffon_path=str(tmp_path))
    inst = tmp_path / "i"
    inst.mkdir()
    (inst / ".restore_r1.json").write_text('{"restore_id": "r1", "status": "success"}')
    posts = []
    monkeypatch.setattr(
        backup, "_post_callback",
        lambda settings, iid, action, payload: posts.append(action) or True)
    backup.reconcile_on_boot(s)
    assert posts == ["restore-result"]
    assert not (inst / ".restore_r1.json").exists()  # removed on ack
