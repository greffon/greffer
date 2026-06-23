"""Epic B slice 1 — greffer-consume of a manager-brokered backup destination.

A backup/restore request may carry a ``destination`` block (per-tenant repo +
creds). When present the greffer must write restic to THAT repo instead of its
own env repo; when absent the self-managed env path is byte-identical to before.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from pydantic import ValidationError

from app.models.controller import BackupDestinationBlock

from app import backup
from app.backup import _effective_settings, restic_env


def _settings(**kw):
    base = dict(
        greffer_id="g1", greffer_token="tok", greffon_path="/tmp",
        greffer_backup_repo="s3:https://env/repo", restic_password="envpw",
        restic_password_file=None, aws_access_key_id="envkey",
        aws_secret_access_key="envsecret",
        restic_sidecar_image="restic/restic:0.16.4",
        backup_stop_timeout_seconds=5, backup_keep_daily=7, backup_keep_weekly=4,
        backup_keep_monthly=6, backup_safety_keep_last=3,
        backup_forget_timeout_seconds=300,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _dest(repo="s3:https://b2/bucket/tenant1", password="tenantpw",
          key="t1key", secret="t1secret"):
    return SimpleNamespace(repo=repo, restic_password=password,
                           aws_access_key_id=key, aws_secret_access_key=secret)


# --- the proxy / restic_env wiring --------------------------------------------
def test_effective_settings_none_is_passthrough():
    s = _settings()
    assert _effective_settings(s, None) is s


def test_brokered_restic_env_targets_destination():
    env = restic_env(_effective_settings(_settings(), _dest()))
    assert env["RESTIC_REPOSITORY"] == "s3:https://b2/bucket/tenant1"
    assert env["RESTIC_PASSWORD"] == "tenantpw"
    assert env["AWS_ACCESS_KEY_ID"] == "t1key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "t1secret"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_brokered_password_overrides_env_password_file():
    # A brokered inline password must beat the greffer's env password FILE, else
    # restic_env would target the file and write with the wrong repo password.
    env = restic_env(_effective_settings(
        _settings(restic_password_file="/run/secrets/pw"), _dest()))
    assert env["RESTIC_PASSWORD"] == "tenantpw"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_brokered_falls_back_to_env_aws_when_dest_omits_creds():
    env = restic_env(_effective_settings(_settings(), _dest(key=None, secret=None)))
    assert env["AWS_ACCESS_KEY_ID"] == "envkey"
    assert env["AWS_SECRET_ACCESS_KEY"] == "envsecret"


def test_non_repo_settings_delegate_to_real_settings():
    eff = _effective_settings(_settings(greffer_id="gX"), _dest())
    assert eff.greffer_id == "gX"
    assert eff.greffon_path == "/tmp"
    assert eff.backup_stop_timeout_seconds == 5


# --- end-to-end through backup_instance ---------------------------------------
def _patch_common(monkeypatch):
    monkeypatch.setattr(backup.compose, "get_status", lambda _id: {"status": "running"})
    monkeypatch.setattr(backup.compose, "stop", mock.Mock())
    monkeypatch.setattr(backup, "_wait_stopped", lambda *a: True)
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_db"])
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())


def _capture_backup_repo(monkeypatch):
    seen = {}

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "backup":
            seen["repo"] = settings.greffer_backup_repo
            seen["password"] = settings.restic_password
            return (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', "")
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    return seen


def test_backup_instance_routes_to_destination_repo(monkeypatch):
    _patch_common(monkeypatch)
    seen = _capture_backup_repo(monkeypatch)
    backup.backup_instance(_settings(), "i", "b1", destination=_dest())
    assert seen["repo"] == "s3:https://b2/bucket/tenant1"
    assert seen["password"] == "tenantpw"


def test_backup_instance_self_managed_uses_env_repo(monkeypatch):
    _patch_common(monkeypatch)
    seen = _capture_backup_repo(monkeypatch)
    backup.backup_instance(_settings(), "i", "b1")  # no destination
    assert seen["repo"] == "s3:https://env/repo"
    assert seen["password"] == "envpw"


def test_restore_instance_safety_snapshot_targets_destination(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(backup, "_restore_volumes", lambda *a, **k: None, raising=False)
    seen = {}

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "backup":  # the safety snapshot
            seen["repo"] = settings.greffer_backup_repo
            return (0, '{"message_type":"summary","snapshot_id":"safe","data_added":1}', "")
        return (0, "", "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    try:
        backup.restore_instance(_settings(), "i", "snap-x", "r1", destination=_dest())
    except Exception:
        pass  # later restore stages may need more patching; we only assert the repo
    assert seen.get("repo") == "s3:https://b2/bucket/tenant1"


# --- repo scheme hardening (review: a buggy/compromised manager must not be able
#     to redirect a tenant backup to a non-S3 restic backend) ---------------------
def test_destination_block_accepts_s3_repo():
    blk = BackupDestinationBlock(repo="s3:https://b2/bucket/t1", restic_password="pw")
    assert blk.repo == "s3:https://b2/bucket/t1"


@pytest.mark.parametrize("bad", [
    "local:/data/repo", "sftp:host:/repo", "rest:http://h/", "/abs/path", "b2:bucket",
])
def test_destination_block_rejects_non_s3_repo(bad):
    with pytest.raises(ValidationError):
        BackupDestinationBlock(repo=bad, restic_password="pw")
