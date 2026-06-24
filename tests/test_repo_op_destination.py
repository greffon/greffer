"""Epic B F1 — per-tenant prune/check: repo-ops run against a manager-brokered
destination, with a PER-REPO lock so different tenants' repos don't contend."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from app import backup
from app.backup import _effective_settings, _repo_op_lock


def _settings(**kw):
    base = dict(
        greffer_id="g1", greffer_token="tok", greffon_path="/tmp",
        greffer_backup_repo="s3:https://env/repo", restic_password="envpw",
        restic_password_file=None, aws_access_key_id=None, aws_secret_access_key=None,
        restic_sidecar_image="restic/restic:0.16.4",
        backup_prune_timeout_seconds=7200, backup_check_timeout_seconds=7200,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _dest(repo="s3:https://b2/bucket/t1", password="tpw", key="k", secret="s"):
    return SimpleNamespace(repo=repo, restic_password=password,
                           aws_access_key_id=key, aws_secret_access_key=secret)


def test_repo_op_lock_is_per_repo():
    a1 = _repo_op_lock("s3:repoA")
    a2 = _repo_op_lock("s3:repoA")
    b = _repo_op_lock("s3:repoB")
    assert a1 is a2        # same repo -> same lock (serializes)
    assert a1 is not b     # different repos -> independent locks


def test_prune_runs_against_destination_repo(monkeypatch):
    seen = {}

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args and args[0] == "prune":
            seen["repo"] = settings.greffer_backup_repo
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    eff = _effective_settings(_settings(), _dest())
    assert backup.prune_repo(eff)["status"] == "success"
    assert seen["repo"] == "s3:https://b2/bucket/t1"   # the tenant repo, not env


def test_check_runs_against_destination_repo(monkeypatch):
    seen = {}
    monkeypatch.setattr(backup, "_run_restic",
                        lambda s, a, m, *, read_only, timeout=3600:
                        (seen.update(repo=s.greffer_backup_repo) or (0, "", "")))
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    backup.check_repo(_effective_settings(_settings(), _dest(repo="s3:tenant9")))
    assert seen["repo"] == "s3:tenant9"


def test_spawn_repo_op_same_repo_409s(monkeypatch):
    # If the SAME repo already has an op running, a second spawn 409s (BusyError).
    s = _settings(greffer_backup_repo="s3:busyrepo")
    lock = _repo_op_lock("s3:busyrepo")
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_repo_op(s, "prune")
    finally:
        lock.release()


def test_spawn_repo_op_different_repos_do_not_contend(monkeypatch):
    # A held env-repo lock must NOT block a DIFFERENT tenant repo's prune.
    monkeypatch.setattr(backup.threading, "Thread", mock.Mock())  # don't run the job
    s = _settings(greffer_backup_repo="s3:envrepo")
    env_lock = _repo_op_lock("s3:envrepo")
    assert env_lock.acquire(blocking=False)
    try:
        backup.spawn_repo_op(s, "prune", destination=_dest(repo="s3:tenantX"))
    finally:
        env_lock.release()
        _repo_op_lock("s3:tenantX").release()   # spawn acquired it (mocked thread)


def test_spawn_repo_op_uninitialized_repo_raises(monkeypatch):
    with pytest.raises(backup.BackupError):
        backup.spawn_repo_op(_settings(greffer_backup_repo=""), "prune")


def test_repo_op_lock_is_reaped_after_completion(monkeypatch):
    # The per-repo lock must be dropped from the registry after the op, so the
    # dict can't grow one entry per tenant repo forever.
    import time
    monkeypatch.setattr(backup, "prune_repo", lambda s: {"status": "success"})
    repo = "s3:reap-me-please"
    backup.spawn_repo_op(_settings(greffer_backup_repo=repo), "prune")
    for _ in range(300):
        with backup._REPO_OP_LOCKS_GUARD:
            if repo not in backup._REPO_OP_LOCKS:
                return
        time.sleep(0.01)
    pytest.fail("per-repo lock was not reaped from the registry")
