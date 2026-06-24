"""Phase 3 hot DB restore orchestration (HLD A4): a manifest with db:<service>
entries triggers start -> wait-healthy -> pg_restore, leaving the instance
RUNNING; any failure stops it and leaves the safety snapshot id for rollback."""
from unittest import mock

from app import backup
from tests.test_controller_backup import _patch_common, _settings


def _db_container(service="postgres", health="healthy",
                  hook="pg_restore -d app", running=True, mount="i_db"):
    c = mock.Mock(id="pgc", status="running" if running else "exited")
    labels = {"com.docker.compose.service": service}
    if hook:
        labels["com.greffon.backup.restore"] = hook
    c.labels = labels
    c.attrs = {"State": {"Health": {"Status": health}},
               "Mounts": ([{"Type": "volume", "Name": mount}] if mount else [])}
    return c


def _run_ok(settings, args, mounts, *, read_only, timeout=3600):
    if args[0] == "backup":  # the safety snapshot
        return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
    return (0, "", "")       # the data restore


_MANIFEST = {"data": "DATA", "db:postgres": "DUMP"}
_CLASSES = {"files": "data", "db": "database"}


def _restore(monkeypatch, **over):
    """Drive a DB restore with all collaborators patched; returns the callback."""
    _patch_common(monkeypatch, volumes=("i_files", "i_db"))
    monkeypatch.setattr(backup, "_run_restic", over.get("run", _run_ok))
    monkeypatch.setattr(backup, "_restart", over.get("restart", mock.Mock()))
    monkeypatch.setattr(backup, "_start_services",
                        over.get("start_services", mock.Mock()))
    monkeypatch.setattr(backup.compose, "stop", over.get("stop", mock.Mock()))
    monkeypatch.setattr(backup.observe, "list_instance_containers",
                        lambda _id: over.get("containers", [_db_container()]))
    monkeypatch.setattr(backup, "_restore_database",
                        over.get("rdb", mock.Mock()))
    monkeypatch.setattr(backup, "_wait_db_healthy",
                        over.get("wait", lambda *a: True))
    monkeypatch.setattr(backup, "_forget", lambda *a, **k: None)
    cb = mock.Mock()
    monkeypatch.setattr(backup, "_post_callback", cb)
    backup.restore_instance(
        _settings(), "i", "snap-1", "r1",
        manifest=over.get("manifest", _MANIFEST),
        volume_classes=over.get("classes", _CLASSES))
    return cb


def test_db_restore_happy_leaves_running(monkeypatch):
    restart = mock.Mock()
    start_services = mock.Mock()
    rdb = mock.Mock()
    cb = _restore(monkeypatch, restart=restart, start_services=start_services,
                  rdb=rdb)
    payload = cb.call_args.args[3]
    assert payload["status"] == "success"
    assert payload["already_running"] is True   # manager must NOT re-start
    assert payload["safety_restic_snapshot_id"] == "SAFE"
    # DB-only start for the restore window, then full instance up AFTER pg_restore
    start_services.assert_called_once()
    assert start_services.call_args.args[2] == ["postgres"]  # only the DB service
    restart.assert_called_once()                # full instance up at the end
    rdb.assert_called_once()
    # _restore_database got the DB dump snapshot + the deterministic filename
    args = rdb.call_args.args
    assert args[3] == "DUMP" and args[4] == "i/postgres.dump"


def test_db_restore_refuses_if_db_volume_in_delete_set(monkeypatch):
    # P0: the class map mislabels the DB volume as 'data', so it would be mounted
    # into the --delete restore and WIPED. The docker-state guard (the DB
    # container's actual mounts) must refuse BEFORE the destructive restore runs.
    calls = []

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        calls.append(args[0])
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (0, "", "")

    cb = _restore(monkeypatch, run=_run,
                  classes={"files": "data", "db": "data"})  # db MISLABELED
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "db_volume_misclassified"
    assert payload["safety_restic_snapshot_id"] == "SAFE"
    assert "restore" not in calls   # the destructive --delete NEVER ran


def test_db_restore_uses_data_only_mounts(monkeypatch):
    # The DATA restore must NOT mount the DB volume (--delete would wipe it; it is
    # repopulated by pg_restore). Capture the restore mounts.
    seen = {}

    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        seen["restore"] = [m[0] for m in mounts]
        return (0, "", "")

    _restore(monkeypatch, run=_run)
    assert "i_files" in seen["restore"]      # data volume restored
    assert "i_db" not in seen["restore"]     # DB volume left for pg_restore


def test_db_restore_db_not_ready_stops_and_fails(monkeypatch):
    stop = mock.Mock()
    cb = _restore(monkeypatch, wait=lambda *a: False, stop=stop)
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "db_not_ready"
    assert payload["safety_restic_snapshot_id"] == "SAFE"
    # started for the DB, then STOPPED on failure (no corrupt DB left running)
    stop.assert_called()


def test_db_restore_missing_hook_fails(monkeypatch):
    cb = _restore(monkeypatch, containers=[_db_container(hook=None)])
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "no_restore_hook"


def test_db_restore_pg_restore_failure_stops_and_keeps_safety(monkeypatch):
    def _boom(*a, **k):
        raise backup.BackupError("restore_failed")

    stop = mock.Mock()
    cb = _restore(monkeypatch, rdb=_boom, stop=stop)
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "restore_failed"
    assert payload["safety_restic_snapshot_id"] == "SAFE"
    stop.assert_called()  # corrupt/partial DB not left running


def test_db_restore_without_classes_refused(monkeypatch):
    # Can't tell data volumes from the DB volume -> would risk wiping the DB.
    restart = mock.Mock()
    cb = _restore(monkeypatch, classes=None, restart=restart)
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "volume_unclassified"
    restart.assert_called_once()  # pre-overwrite failure -> restore service


def test_db_restore_data_failure_before_start(monkeypatch):
    # The data restore fails before the DB is touched -> instance NOT started,
    # left stopped with safety_id.
    def _run(settings, args, mounts, *, read_only, timeout=3600):
        if args[0] == "backup":
            return (0, '{"message_type":"summary","snapshot_id":"SAFE"}', "")
        return (1, "", "disk full")  # data restore fails

    restart = mock.Mock()
    start_services = mock.Mock()
    rdb = mock.Mock()
    cb = _restore(monkeypatch, run=_run, restart=restart,
                  start_services=start_services, rdb=rdb)
    payload = cb.call_args.args[3]
    assert payload["status"] == "failed"
    assert payload["error_code"] == "disk_full"
    start_services.assert_not_called()  # never started (data overwrite failed)
    restart.assert_not_called()
    rdb.assert_not_called()             # never reached pg_restore
