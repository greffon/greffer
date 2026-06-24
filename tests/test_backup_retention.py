from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

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
        backup_safety_keep_last=3, backup_forget_timeout_seconds=300,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_common(monkeypatch, status="running", wait=True, volumes=("i_db",)):
    monkeypatch.setattr(backup.compose, "get_status", lambda _id: {"status": status})
    monkeypatch.setattr(backup.compose, "stop", mock.Mock())
    monkeypatch.setattr(backup, "_wait_stopped", lambda *a: wait)
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: list(volumes))
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)


def test_backup_forgets_instance_tag(monkeypatch):
    _patch_common(monkeypatch)
    calls = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        calls.append(args)
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', "")
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1")

    assert cb.call_args.args[3]["status"] == "success"
    forgets = [a for a in calls if a[0] == "forget"]
    assert len(forgets) == 1
    f = forgets[0]
    assert "--tag" in f and "instance:i" in f      # tag-isolated to the instance ns
    assert "safety:i" not in f                      # never the safety ns
    assert f[f.index("--keep-daily") + 1] == "7"
    assert "--keep-weekly" in f and "--keep-monthly" in f
    assert "--group-by" in f and "tags" in f        # grouping pinned


def test_forget_runs_after_restart_off_downtime_path(monkeypatch):
    # retention must run AFTER _restart so a hung forget can't extend downtime
    _patch_common(monkeypatch)
    order = []
    monkeypatch.setattr(backup, "_restart", lambda *a: order.append("restart"))

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "forget":
            order.append("forget")
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"S"}', "")
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())
    backup.backup_instance(_settings(), "i", "b1")
    assert order == ["restart", "forget"]


def test_forget_failure_does_not_fail_backup(monkeypatch):
    _patch_common(monkeypatch)

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"S"}', "")
        if args[0] == "forget":
            return (1, "", "repo busy")     # retention fails
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)
    backup.backup_instance(_settings(), "i", "b1")
    assert cb.call_args.args[3]["status"] == "success"   # backup still succeeds


def test_no_retention_on_failed_backup(monkeypatch):
    _patch_common(monkeypatch, wait=False)   # stop timeout -> no snapshot -> failed
    calls = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        calls.append(args[0])
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())
    backup.backup_instance(_settings(), "i", "b1")
    assert "forget" not in calls            # a failed backup runs no retention


def test_restore_bounds_safety_tag_after_overwrite(monkeypatch):
    _patch_common(monkeypatch)
    calls = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        calls.append(args)
        if args[0] == "backup":            # the safety snapshot
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())
    backup.restore_instance(_settings(), "i", "snap-1", "r1")

    # safety snapshot, THEN overwrite, THEN retention (off the pre-overwrite path)
    assert [a[0] for a in calls] == ["backup", "restore", "forget"]
    f = [a for a in calls if a[0] == "forget"][0]
    assert "safety:i" in f and "--keep-last" in f and "3" in f
    assert "instance:i" not in f                      # never the instance ns


def test_negative_safety_keep_last_floored_to_one(monkeypatch):
    # a NEGATIVE keep-last must be floored to 1 so retention can never delete the
    # just-created safety snapshot (the rollback point).
    _patch_common(monkeypatch)
    calls = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        calls.append(args)
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())
    backup.restore_instance(_settings(backup_safety_keep_last=-1), "i", "snap", "r1")
    f = [a for a in calls if a[0] == "forget"][0]
    assert f[f.index("--keep-last") + 1] == "1"        # floored, not "-1"


def test_forget_swallows_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("docker gone")

    monkeypatch.setattr(backup, "_run_restic", _boom)
    backup._forget(_settings(), "i", safety=False)     # best-effort: must NOT raise
