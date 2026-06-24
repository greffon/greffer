from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app import backup


def _settings(**kw):
    base = dict(
        greffer_id="g1", greffon_path="/tmp",
        greffer_backup_repo="s3:https://h/repo", restic_password="pw",
        restic_password_file=None, aws_access_key_id=None,
        aws_secret_access_key=None, restic_sidecar_image="restic/restic:0.16.4",
        backup_prune_timeout_seconds=7200, backup_check_timeout_seconds=7200,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_prune_success(monkeypatch):
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup, "_run_restic", lambda *a, **k: (0, "", ""))
    assert backup.prune_repo(_settings())["status"] == "success"


def test_prune_failure_classified(monkeypatch):
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup, "_run_restic",
                        lambda *a, **k: (1, "", "no space left on device"))
    out = backup.prune_repo(_settings())
    assert out["status"] == "failed" and out["error_code"] == "disk_full"


def test_prune_repo_busy_is_retryable(monkeypatch):
    # a concurrent backup sidecar holds restic's exclusive lock -> a clean
    # retry-next-cadence code, NOT a hard failure.
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (1, "", "repository is already locked exclusively"))
    assert backup.prune_repo(_settings())["error_code"] == "repo_busy"


def test_prune_uses_prune_arg_and_its_timeout(monkeypatch):
    captured = {}

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        captured["args"], captured["timeout"] = args, timeout
        return (0, "", "")

    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup, "_run_restic", _run)
    backup.prune_repo(_settings(backup_prune_timeout_seconds=99))
    assert captured["args"] == ["prune"] and captured["timeout"] == 99


def test_check_success_uses_check_arg(monkeypatch):
    captured = {}

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        captured["args"] = args
        return (0, "", "")

    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup, "_run_restic", _run)
    assert backup.check_repo(_settings())["status"] == "success"
    assert captured["args"] == ["check"]


def test_check_repo_busy_is_retryable(monkeypatch):
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (1, "", "unable to create lock in backend: "
                                "repository is already locked"))
    assert backup.check_repo(_settings())["error_code"] == "repo_busy"


def test_spawn_repo_op_busy_raises():
    backup._repo_op_lock("s3:https://h/repo").acquire()
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_repo_op(_settings(), "prune")
    finally:
        backup._repo_op_lock("s3:https://h/repo").release()


def test_spawn_repo_op_runs_and_releases_the_lock(monkeypatch):
    done = threading.Event()

    def _prune(settings):
        done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "prune_repo", _prune)
    backup.spawn_repo_op(_settings(), "prune")
    assert done.wait(timeout=5)
    # the _locked_job finally releases the lock just after the op returns; poll.
    for _ in range(200):
        if backup._repo_op_lock("s3:https://h/repo").acquire(blocking=False):
            backup._repo_op_lock("s3:https://h/repo").release()
            break
        time.sleep(0.01)
    else:
        pytest.fail("repo-op lock was not released after the op")


def test_spawn_repo_op_dispatches_check(monkeypatch):
    done = threading.Event()

    def _check(settings):
        done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "check_repo", _check)
    backup.spawn_repo_op(_settings(), "check")
    assert done.wait(timeout=5)
    for _ in range(200):
        if backup._repo_op_lock("s3:https://h/repo").acquire(blocking=False):
            backup._repo_op_lock("s3:https://h/repo").release()
            break
        time.sleep(0.01)
    else:
        pytest.fail("repo-op lock was not released after the check op")
