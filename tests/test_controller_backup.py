from __future__ import annotations

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
        aws_secret_access_key=None, restic_sidecar_image="restic/restic:0.16.4",
        backup_stop_timeout_seconds=5,
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

    def _run(settings, args, mounts, *, read_only):
        order.append(args[0])
        if args[0] == "backup":   # the safety snapshot
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (0, "", "")        # the restore

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.restore_instance(_settings(), "i", "snap-1", "r1")

    assert order == ["backup", "restore"]   # safety FIRST
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

    def _run(settings, args, mounts, *, read_only):
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

def test_spawn_backup_busy_raises(monkeypatch):
    lock = backup._instance_lock("busy-i")
    lock.acquire()
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_backup(_settings(), "busy-i", "b1")
    finally:
        lock.release()
