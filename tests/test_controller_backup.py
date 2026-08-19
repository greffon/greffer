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

def _drain(monkeypatch, spawn, instance_id, *args, **kwargs):
    """Run ``spawn`` and block until its background job has finished, so the
    assertions below see the job's final lock state rather than a race."""
    done = threading.Event()
    real_job = backup._locked_job

    def _job(release, fn, *a, **kw):
        try:
            real_job(release, fn, *a, **kw)
        finally:
            done.set()

    monkeypatch.setattr(backup, "_locked_job", _job)
    spawn(_settings(), instance_id, *args, **kwargs)
    assert done.wait(20), "background job hung"


def _lock_state_at_callback(monkeypatch):
    """Record whether the per-instance lock is free while the result callback is
    in flight -- i.e. whether the manager's inline follow-up call would be served
    or refused 409 instance_busy."""
    seen = {}

    def _cb(settings, iid, action, payload):
        lock = backup._instance_lock(iid)
        free = lock.acquire(blocking=False)
        if free:
            lock.release()
        seen[action] = free
        return True

    monkeypatch.setattr(backup, "_post_callback", _cb)
    return seen


def test_spawn_backup_busy_raises(monkeypatch):
    lock = backup._instance_lock("busy-i")
    lock.acquire()
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_backup(_settings(), "busy-i", "b1")
    finally:
        lock.release()


def test_restore_releases_the_lock_before_its_callback(monkeypatch):
    """The manager answers a successful restore-result INLINE by POSTing back to
    /api/controller/start/, which takes this same per-instance lock. Holding the
    lock across the callback made that start a guaranteed 409 instance_busy, so
    every data-path restore landed as ``restored_start_failed`` with the instance
    left stopped."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"SAFE"}', ""))
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", mock.Mock())
    monkeypatch.setattr(backup, "_write_json", mock.Mock())
    monkeypatch.setattr(backup, "_remove", mock.Mock())
    seen = _lock_state_at_callback(monkeypatch)

    _drain(monkeypatch, backup.spawn_restore, "rel-restore", "snap-1", "r1")

    assert seen["restore-result"] is True, (
        "the restore job still held the per-instance lock while calling back -- "
        "the manager's inline start would be refused 409 instance_busy")
    # And the job still gave the lock back exactly once.
    assert backup._instance_lock("rel-restore").acquire(blocking=False)
    backup._instance_lock("rel-restore").release()


def test_backup_releases_the_lock_before_its_callback(monkeypatch):
    """Same rule on the backup side: the migration cutover polls the snapshot to
    terminal -- which this callback is what makes it -- and then acts on the
    instance immediately."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', ""))
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", mock.Mock())
    monkeypatch.setattr(backup, "_remove", mock.Mock())
    seen = _lock_state_at_callback(monkeypatch)

    _drain(monkeypatch, backup.spawn_backup, "rel-backup", "b1")

    assert seen["backup-result"] is True
    assert backup._instance_lock("rel-backup").acquire(blocking=False)
    backup._instance_lock("rel-backup").release()


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
