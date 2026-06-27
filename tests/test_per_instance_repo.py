"""Per-instance self-host repos: when GREFFER_BACKUP_REPO_PER_INSTANCE is on, the
env repo is a base and each instance's repo is <base>/<instance_id> -- the basis
for self-host cross-greffer migration (a second greffer on the same base reads it)."""
import pytest

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
    # _effective_settings with no instance_id stays the base; the per-instance
    # SPACE reclaim happens via spawn_repo_op's fan-out, not here (see the
    # fan-out tests below).
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


# ---- prune/check fan-out across per-instance sub-repos ---------------------
#
# The SPACE-reclaim gap: in per-instance mode each instance's backups live at
# <base>/<id>, but a bare-<base> prune/check would touch none of them, so disk
# grows unbounded. spawn_repo_op must fan out across the sub-repos.

import threading  # noqa: E402
import time  # noqa: E402


def _drain_base_lock(base):
    """Wait for the fan-out orchestrator thread to finish (it releases the base
    lock in a finally), then drop it so the next test starts clean."""
    lock = backup._repo_op_lock(base)
    for _ in range(500):
        if lock.acquire(blocking=False):
            backup._reap_repo_op_lock(base, lock)
            return
        time.sleep(0.01)
    raise AssertionError("fan-out orchestrator never released the base lock")


def test_fanout_prunes_each_instance_subrepo(monkeypatch):
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    monkeypatch.setattr(backup, "_list_instance_ids", lambda _s: ["a", "b", "c"])
    monkeypatch.setattr(backup, "_repo_missing", lambda _s: False)
    seen, done = [], threading.Event()

    def _prune(settings):
        seen.append(settings.greffer_backup_repo)
        if len(seen) == 3:
            done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "prune_repo", _prune)
    backup.spawn_repo_op(s, "prune")
    assert done.wait(timeout=5)
    _drain_base_lock(base)
    # each sub-repo pruned -- NOT the bare base.
    assert sorted(seen) == [f"{base}/a", f"{base}/b", f"{base}/c"]
    assert base not in seen


def test_fanout_skips_never_backed_up_instances(monkeypatch):
    # An instance with no sub-repo yet must be SKIPPED, not init'd -- the sweep
    # must never litter the base with empty <base>/<id> repos.
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    monkeypatch.setattr(backup, "_list_instance_ids", lambda _s: ["live", "virgin"])
    monkeypatch.setattr(
        backup, "_repo_missing",
        lambda eff: eff.greffer_backup_repo != f"{base}/live")
    pruned, done = [], threading.Event()

    def _prune(settings):
        pruned.append(settings.greffer_backup_repo)
        done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "prune_repo", _prune)
    backup.spawn_repo_op(s, "prune")
    assert done.wait(timeout=5)
    _drain_base_lock(base)
    assert pruned == [f"{base}/live"]


def test_fanout_one_failing_instance_does_not_abort_the_rest(monkeypatch):
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    monkeypatch.setattr(backup, "_list_instance_ids", lambda _s: ["a", "b", "c"])
    monkeypatch.setattr(backup, "_repo_missing", lambda _s: False)
    pruned, done = [], threading.Event()

    def _prune(settings):
        repo = settings.greffer_backup_repo
        if repo == f"{base}/b":
            raise RuntimeError("boom")
        pruned.append(repo)
        if repo == f"{base}/c":
            done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "prune_repo", _prune)
    backup.spawn_repo_op(s, "prune")
    assert done.wait(timeout=5)
    _drain_base_lock(base)
    assert sorted(pruned) == [f"{base}/a", f"{base}/c"]  # 'b' raised, rest ran


def test_fanout_dispatches_check(monkeypatch):
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    monkeypatch.setattr(backup, "_list_instance_ids", lambda _s: ["a"])
    monkeypatch.setattr(backup, "_repo_missing", lambda _s: False)
    seen, done = [], threading.Event()

    def _check(settings):
        seen.append(settings.greffer_backup_repo)
        done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "check_repo", _check)
    backup.spawn_repo_op(s, "check")
    assert done.wait(timeout=5)
    _drain_base_lock(base)
    assert seen == [f"{base}/a"]


def test_fanout_busy_raises_on_overlapping_sweep():
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    backup._repo_op_lock(base).acquire()
    try:
        with pytest.raises(backup.BusyError):
            backup.spawn_repo_op(s, "prune")
    finally:
        backup._reap_repo_op_lock(base, backup._repo_op_lock(base))


def test_fanout_no_instances_is_a_noop(monkeypatch):
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    monkeypatch.setattr(backup, "_list_instance_ids", lambda _s: [])
    monkeypatch.setattr(backup, "_repo_missing", lambda _s: False)
    called = []
    monkeypatch.setattr(backup, "prune_repo", lambda s: called.append(1))
    backup.spawn_repo_op(s, "prune")
    _drain_base_lock(base)  # orchestrator started, found nothing, released cleanly
    assert called == []


def test_flag_off_does_not_fan_out(monkeypatch):
    # Flag off -> the single-repo path: prune the env repo itself, no enumeration.
    s = _settings(greffer_backup_repo="s3:https://h/repo",
                  greffer_backup_repo_per_instance=False)
    monkeypatch.setattr(
        backup, "_list_instance_ids",
        lambda _s: (_ for _ in ()).throw(AssertionError("should not enumerate")))
    seen, done = [], threading.Event()

    def _prune(settings):
        seen.append(settings.greffer_backup_repo)
        done.set()
        return {"status": "success"}

    monkeypatch.setattr(backup, "prune_repo", _prune)
    backup.spawn_repo_op(s, "prune")
    assert done.wait(timeout=5)
    _drain_base_lock("s3:https://h/repo")
    assert seen == ["s3:https://h/repo"]


# ---- _repo_missing: skip ONLY genuinely-missing repos --------------------


def test_repo_missing_true_on_exit_code_10(monkeypatch):
    # restic >= 0.17 exit 10 == "repository does not exist" -- the ONLY skip case.
    s = _settings(greffer_backup_repo="s3:https://h/bucket/i")
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (10, "", "Fatal: unable to open config file: "
                                 "The specified key does not exist."))
    assert backup._repo_missing(s) is True


def test_repo_missing_false_on_present(monkeypatch):
    s = _settings(greffer_backup_repo="s3:https://h/bucket/i")
    monkeypatch.setattr(backup, "_run_restic", lambda *a, **k: (0, "{}", ""))
    assert backup._repo_missing(s) is False


def test_repo_missing_false_on_access_or_backend_failure(monkeypatch):
    # A wrong-password / backend-outage / GONE-BUCKET / corrupt-config failure is
    # NOT "missing" (exit != 10) -- it must return False so the fan-out falls
    # through to prune/check and logs a CLASSIFIED failure instead of silently
    # skipping. NoSuchBucket is the trap: its stderr CONTAINS "does not exist", so
    # a substring check would wrongly skip the whole fan-out -- the exit code (1,
    # not 10) keeps it loud.
    s = _settings(greffer_backup_repo="s3:https://h/bucket/i")
    cases = [
        (12, "wrong password or no key found"),
        (1, "dial tcp: connection refused"),
        (1, "Head: AccessDenied: Access Denied"),
        (1, "Fatal: unable to open config file: NoSuchBucket: The specified "
            "bucket does not exist."),
    ]
    for rc, stderr in cases:
        monkeypatch.setattr(backup, "_run_restic",
                            lambda *a, _r=rc, _e=stderr, **k: (_r, "", _e))
        assert backup._repo_missing(s) is False, (rc, stderr)


def test_fanout_does_not_skip_existing_subrepo_on_access_failure(monkeypatch):
    # End to end: under a global credential/backend problem, every sub-repo must
    # still be handed to prune (which logs the classified failure), NOT skipped.
    base = "s3:https://h/bucket"
    s = _settings(greffer_backup_repo=base, greffer_backup_repo_per_instance=True)
    monkeypatch.setattr(backup, "_list_instance_ids", lambda _s: ["a", "b"])
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (1, "", "wrong password or no key found"))
    pruned, done = [], threading.Event()

    def _prune(settings):
        pruned.append(settings.greffer_backup_repo)
        if len(pruned) == 2:
            done.set()
        return {"status": "failed", "error_code": "auth_failed"}

    monkeypatch.setattr(backup, "prune_repo", _prune)
    backup.spawn_repo_op(s, "prune")
    assert done.wait(timeout=5)
    _drain_base_lock(base)
    assert sorted(pruned) == [f"{base}/a", f"{base}/b"]  # neither silently skipped
