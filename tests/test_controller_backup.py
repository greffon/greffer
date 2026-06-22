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

    def _run(settings, args, mounts, *, read_only):
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
