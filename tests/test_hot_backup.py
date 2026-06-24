"""Phase 3 hot-backup tests: the data-volume hot path (no stop, restic-live the
data-class volumes, skip regenerable) and that cold is unchanged without
volume_classes."""
from unittest import mock

import pytest

from app import backup
from tests.test_controller_backup import _patch_common, _settings


def test_hot_backup_mounts_only_data_class(monkeypatch):
    monkeypatch.setattr(backup, "_data_volumes",
                        lambda _id: ["i_db", "i_cache", "i_uploads"])
    mounts = backup._hot_backup_mounts(
        _settings(), "i", {"db": "data", "cache": "regenerable", "uploads": "data"})
    # only DATA-class volumes (db, uploads); cache (regenerable) skipped; the
    # class is looked up by the COMPOSE name (the <id>_ prefix stripped).
    assert mounts == [("i_db", "/data/i_db"), ("i_uploads", "/data/i_uploads")]


def test_hot_backup_does_not_stop_or_restart(monkeypatch):
    _patch_common(monkeypatch, status="running", volumes=("i_db",))
    stop = mock.Mock()
    monkeypatch.setattr(backup.compose, "stop", stop)
    restart = mock.Mock()
    monkeypatch.setattr(backup, "_restart", restart)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"S","data_added":7}', ""))
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1", volume_classes={"db": "data"})

    stop.assert_not_called()      # HOT keeps the instance running
    restart.assert_not_called()   # never stopped -> never restart
    assert cb.call_args.args[3]["status"] == "success"


def test_hot_backup_passes_only_data_volumes_to_restic(monkeypatch):
    _patch_common(monkeypatch, status="running", volumes=("i_db", "i_cache"))
    captured = {}

    def _run(settings, args, mounts, **k):
        captured["mounts"] = mounts
        return (0, '{"message_type":"summary","snapshot_id":"S","data_added":1}', "")

    monkeypatch.setattr(backup, "_run_restic", _run)
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())
    monkeypatch.setattr(backup, "_restart", mock.Mock())

    backup.backup_instance(_settings(), "i", "b1",
                           volume_classes={"db": "data", "cache": "regenerable"})

    vols = [m[0] for m in captured["mounts"]]
    assert "i_db" in vols and "i_cache" not in vols


def test_hot_mounts_reject_unclassified_volume(monkeypatch):
    # A real data volume the manager didn't classify must NOT be silently dropped.
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_db", "i_logs"])
    with pytest.raises(backup.BackupError):
        backup._hot_backup_mounts(_settings(), "i", {"db": "data"})  # i_logs missing


def test_hot_mounts_reject_empty_data_set(monkeypatch):
    # All-regenerable (or class keys matching nothing) -> no data -> must fail,
    # never an L4-only false-success snapshot.
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_cache"])
    with pytest.raises(backup.BackupError):
        backup._hot_backup_mounts(_settings(), "i", {"cache": "regenerable"})


def test_hot_backup_no_data_reports_failed_not_false_success(monkeypatch):
    # The P1 the review caught: a dataless hot request must report FAILED, not a
    # success the manager would record as a good (empty) backup.
    _patch_common(monkeypatch, status="running", volumes=("i_cache",))
    monkeypatch.setattr(backup, "_run_restic",
                        lambda *a, **k: (0, '{"snapshot_id":"S"}', ""))  # must not be reached
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1",
                           volume_classes={"cache": "regenerable"})

    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "no_data_volumes"


def test_cold_backup_unchanged_without_volume_classes(monkeypatch):
    _patch_common(monkeypatch, status="running", volumes=("i_db",))
    stop = mock.Mock()
    monkeypatch.setattr(backup.compose, "stop", stop)
    restart = mock.Mock()
    monkeypatch.setattr(backup, "_restart", restart)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"S","data_added":1}', ""))
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    monkeypatch.setattr(backup, "_post_callback", mock.Mock())

    backup.backup_instance(_settings(), "i", "b1")  # no volume_classes -> COLD

    stop.assert_called_once()
    restart.assert_called_once()
