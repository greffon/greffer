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
    with pytest.raises(backup.BackupError) as exc:
        backup._hot_backup_mounts(_settings(), "i", {"db": "data"})  # i_logs missing
    assert exc.value.code == "volume_unclassified"


def test_hot_mounts_empty_data_returns_empty(monkeypatch):
    # All-regenerable -> [] (NOT a raise): a DB-only app legitimately has no data
    # volumes; the no-artifact decision moved up to _run_hot_backup.
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_cache"])
    assert backup._hot_backup_mounts(_settings(), "i", {"cache": "regenerable"}) == []


def test_hot_mounts_skips_database_volume(monkeypatch):
    # A database volume is captured by its dump hook, not a raw snapshot -> it is
    # SKIPPED from the restic mounts (not raised as unclassified).
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_files", "i_db"])
    mounts = backup._hot_backup_mounts(
        _settings(), "i", {"files": "data", "db": "database"})
    assert mounts == [("i_files", "/data/i_files")]  # i_db (database) skipped


def _container(service, status="running", dump=None, cid="c1"):
    labels = {"com.docker.compose.service": service}
    if dump:
        labels["com.greffon.backup.dump"] = dump
    return mock.Mock(status=status, labels=labels, id=cid, name=service)


def test_dump_hooks_reads_running_db_service(monkeypatch):
    monkeypatch.setattr(
        backup.observe, "list_instance_containers",
        lambda _id: [_container("web"),
                     _container("db", dump="pg_dump -U app app", cid="dbc")])
    hooks = backup._dump_hooks("i")
    assert len(hooks) == 1  # only the service with the dump label
    service, cid, argv = hooks[0]
    assert service == "db" and cid == "dbc"
    # wrapped in an in-container `timeout` (a hung dump self-kills in the DB
    # container, closing #112's local-only kill); the command is shlex-split argv.
    assert argv == ["timeout", "3600", "pg_dump", "-U", "app", "app"]


def test_dump_hooks_skips_stopped_container(monkeypatch):
    monkeypatch.setattr(
        backup.observe, "list_instance_containers",
        lambda _id: [_container("db", status="exited", dump="pg_dump app")])
    assert backup._dump_hooks("i") == []  # a stopped DB cannot be dumped


def test_run_hot_backup_data_only(monkeypatch):
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_db"])
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"DATA","data_added":5}', ""))
    out = backup._run_hot_backup(_settings(), "i", "b1", {"db": "data"})
    assert out["status"] == "success"
    assert out["snapshot_id"] == "DATA"
    assert out["manifest"] == {"data": "DATA"}
    assert out["bytes_added"] == 5


def test_run_hot_backup_db_only_primary_is_dump(monkeypatch):
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_db"])
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: [_container("db", dump="pg_dump app", cid="dbc")])
    monkeypatch.setattr(backup, "_dump_and_backup",
                        lambda s, i, cid, argv, fn: ("DUMP", 9))
    out = backup._run_hot_backup(_settings(), "i", "b1", {"db": "database"})
    assert out["manifest"] == {"db:db": "DUMP"}
    assert out["snapshot_id"] == "DUMP"  # no data snapshot -> primary is the dump
    assert out["bytes_added"] == 9


def test_run_hot_backup_mixed_data_and_db(monkeypatch):
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_files", "i_db"])
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"DATA","data_added":5}', ""))
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: [_container("db", dump="pg_dump app", cid="dbc")])
    monkeypatch.setattr(backup, "_dump_and_backup",
                        lambda s, i, cid, argv, fn: ("DUMP", 9))
    out = backup._run_hot_backup(
        _settings(), "i", "b1", {"files": "data", "db": "database"})
    assert out["manifest"] == {"data": "DATA", "db:db": "DUMP"}
    assert out["snapshot_id"] == "DATA"  # primary = the data snapshot
    assert out["bytes_added"] == 14


def test_run_hot_backup_database_without_hook_fails_before_snapshot(monkeypatch):
    # A database volume but NO service declares a dump hook -> the DB would be
    # silently lost. Refuse, and BEFORE the data snapshot (no orphan snapshot).
    ran_restic = mock.Mock()
    monkeypatch.setattr(backup, "_data_volumes", lambda _id: ["i_files", "i_db"])
    monkeypatch.setattr(backup, "ensure_repo", lambda s: None)
    monkeypatch.setattr(backup, "_run_restic", ran_restic)
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: [_container("web")])  # no dump hook anywhere
    with pytest.raises(backup.BackupError) as exc:
        backup._run_hot_backup(
            _settings(), "i", "b1", {"files": "data", "db": "database"})
    assert exc.value.code == "no_dump_hook"
    ran_restic.assert_not_called()  # failed before creating an orphan data snapshot


def test_hot_backup_database_end_to_end(monkeypatch):
    _patch_common(monkeypatch, status="running", volumes=("i_files", "i_db"))
    monkeypatch.setattr(
        backup, "_run_restic",
        lambda *a, **k: (0, '{"message_type":"summary","snapshot_id":"DATA","data_added":5}', ""))
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: [_container("db", dump="pg_dump app", cid="dbc")])
    monkeypatch.setattr(backup, "_dump_and_backup",
                        lambda s, i, cid, argv, fn: ("DUMP", 9))
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1",
                           volume_classes={"files": "data", "db": "database"})

    payload = cb.call_args.args[3]
    assert payload["status"] == "success"
    assert payload["manifest"] == {"data": "DATA", "db:db": "DUMP"}


def test_hot_backup_unclassified_reports_failed(monkeypatch):
    # backup_instance-level: an unclassified data volume -> FAILED callback with
    # the right code (the P2-regression guard, end to end).
    _patch_common(monkeypatch, status="running", volumes=("i_db", "i_logs"))
    monkeypatch.setattr(backup, "_run_restic",
                        lambda *a, **k: (0, '{"snapshot_id":"S"}', ""))  # must not be reached
    monkeypatch.setattr(backup, "_restart", mock.Mock())
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)

    backup.backup_instance(_settings(), "i", "b1", volume_classes={"db": "data"})

    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "volume_unclassified"


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
