"""Per-instance self-host repos: when GREFFER_BACKUP_REPO_PER_INSTANCE is on, the
env repo is a base and each instance's repo is <base>/<instance_id> -- the basis
for self-host cross-greffer migration (a second greffer on the same base reads it)."""
from app import backup
from tests.test_controller_backup import _settings


def test_per_instance_derives_subrepo():
    s = _settings(greffer_backup_repo="s3:https://h/bucket",
                  greffer_backup_repo_per_instance=True)
    eff = backup._effective_settings(s, None, "inst-42")
    assert eff.greffer_backup_repo == "s3:https://h/bucket/inst-42"
    # other attrs delegate
    assert eff.restic_password == s.restic_password


def test_trailing_slash_base_no_double_slash():
    s = _settings(greffer_backup_repo="s3:https://h/bucket/",
                  greffer_backup_repo_per_instance=True)
    eff = backup._effective_settings(s, None, "i")
    assert eff.greffer_backup_repo == "s3:https://h/bucket/i"


def test_flag_off_is_flat_repo_unchanged():
    s = _settings(greffer_backup_repo="s3:https://h/bucket",
                  greffer_backup_repo_per_instance=False)
    assert backup._effective_settings(s, None, "i") is s  # unchanged


def test_no_instance_id_is_flat(monkeypatch):
    # spawn_repo_op passes no instance_id -> stays the base (repo-wide op).
    s = _settings(greffer_backup_repo="s3:https://h/bucket",
                  greffer_backup_repo_per_instance=True)
    assert backup._effective_settings(s, None) is s


def test_brokered_destination_ignores_per_instance_flag():
    # A manager-brokered destination is already per-instance-prefixed -> the flag
    # is env-repo-only; the destination repo wins, instance_id not appended.
    from types import SimpleNamespace
    dest = SimpleNamespace(repo="s3:https://m/tenant/inst-42", restic_password="p",
                           aws_access_key_id="k", aws_secret_access_key="s")
    s = _settings(greffer_backup_repo="s3:https://h/bucket",
                  greffer_backup_repo_per_instance=True)
    eff = backup._effective_settings(s, dest, "inst-42")
    assert eff.greffer_backup_repo == "s3:https://m/tenant/inst-42"  # no double append
