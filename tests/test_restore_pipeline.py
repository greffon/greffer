"""Phase 3 hot DB restore: the reverse dual-gated `restic dump <snap> | docker
exec -i <db> pg_restore` pipeline. Both ends gate -- a truncated dump (producer)
OR a failed restore (consumer) must fail so the orchestrator rolls back."""
import subprocess
from unittest import mock

import pytest

from app import backup
from tests.test_controller_backup import _settings


def _pair(prod_rc, cons_rc, timeout=False, prod_hang=False):
    """[producer=restic dump, consumer=pg_restore] mock Popen pair."""
    producer = mock.Mock(returncode=prod_rc)
    producer.stdout = mock.Mock()
    if prod_hang:
        producer.wait.side_effect = subprocess.TimeoutExpired("restic dump", 1)
    consumer = mock.Mock(returncode=cons_rc)
    if timeout:
        consumer.communicate.side_effect = subprocess.TimeoutExpired("pg_restore", 1)
    else:
        consumer.communicate.return_value = ("", "")
    return [producer, consumer]


def _call():
    return backup._restore_database(
        _settings(), "dbcid", ["pg_restore", "-d", "app"], "SNAP", "i/db.dump")


def test_restore_happy_returns_none():
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(0, 0)):
        assert _call() is None


def test_failed_restic_dump_producer_fails_restore():
    # A truncated dump (restic dump rc != 0) feeds pg_restore a partial stream ->
    # the DB is not cleanly restored -> fail (orchestrator rolls back).
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(1, 0)):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "restore_failed"


def test_failed_pg_restore_consumer_fails_restore():
    # A failed restore leaves a half-applied (corrupt) DB -> fail.
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(0, 1)):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "restore_failed"


def test_both_fail_restore_failed():
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(1, 1)):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "restore_failed"


def test_consumer_timeout_kills_both():
    pair = _pair(0, 0, timeout=True)
    with mock.patch("app.backup.subprocess.Popen", side_effect=pair):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "timeout"
    pair[0].kill.assert_called_once()
    pair[1].kill.assert_called_once()
    pair[0].wait.assert_called()  # reaped
    pair[1].wait.assert_called()


def test_producer_hang_is_timeout():
    pair = _pair(0, 0, prod_hang=True)
    with mock.patch("app.backup.subprocess.Popen", side_effect=pair):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "timeout"
    pair[0].kill.assert_called_once()
    pair[1].kill.assert_called_once()


def test_no_secret_in_argv():
    sentinel = "S3cr3t-Sentinel-Value"
    s = _settings(restic_password=sentinel)
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(0, 0)) as P:
        backup._restore_database(s, "dbcid", ["pg_restore"], "SNAP", "i/db.dump")
    for call in P.call_args_list:
        assert sentinel not in " ".join(call.args[0])
