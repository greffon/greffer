"""Phase 3 hot DB backup: the A3 dual-exit-gated `docker exec pg_dump | restic
backup --stdin` streaming pipeline. The load-bearing test is that a TRUNCATED
dump (producer non-zero) fails even though `restic --stdin` exited 0."""
import subprocess
from unittest import mock

import pytest

from app import backup
from tests.test_controller_backup import _settings

_SUMMARY = '{"message_type":"summary","snapshot_id":"S","data_added":42}'
_SUMMARY_EMPTY = '{"message_type":"summary","snapshot_id":"S","data_added":0}'


def _pair(prod_rc, cons_rc, cons_out="", cons_err="",
          timeout=False, prod_hang=False):
    """[producer, consumer] mock Popen pair for subprocess.Popen's side_effect."""
    producer = mock.Mock(returncode=prod_rc)
    producer.stdout = mock.Mock()
    if prod_hang:
        producer.wait.side_effect = subprocess.TimeoutExpired("docker exec", 1)
    consumer = mock.Mock(returncode=cons_rc)
    if timeout:
        consumer.communicate.side_effect = subprocess.TimeoutExpired("restic", 1)
    else:
        consumer.communicate.return_value = (cons_out, cons_err)
    return [producer, consumer]


def _call():
    return backup._dump_and_backup(
        _settings(), "i", "dbcid", ["pg_dump", "-U", "u", "db"], "i/db.sql")


def test_dump_backup_happy_returns_snapshot_and_bytes():
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(0, 0, _SUMMARY)):
        assert _call() == ("S", 42)


def test_dump_failure_gates_even_when_restic_exits_zero():
    # THE A3 TEST: a truncated dump (producer rc != 0) must fail the backup even
    # though restic --stdin happily exited 0 on the partial stream.
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(1, 0, _SUMMARY)):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "dump_failed"


def test_restic_failure_is_classified():
    with mock.patch("app.backup.subprocess.Popen",
                    side_effect=_pair(0, 1, "", "wrong password")):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "auth_failed"


def test_empty_dump_fails_loud():
    # restic exited 0 but data_added=0 -> a zero-byte dump, never a real backup.
    with mock.patch("app.backup.subprocess.Popen",
                    side_effect=_pair(0, 0, _SUMMARY_EMPTY)):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "dump_empty"


def test_consumer_timeout_kills_both_and_fails():
    pair = _pair(0, 0, timeout=True)
    with mock.patch("app.backup.subprocess.Popen", side_effect=pair):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "timeout"
    pair[0].kill.assert_called_once()  # both LOCAL clients killed...
    pair[1].kill.assert_called_once()
    pair[0].wait.assert_called()       # ...and reaped (no local zombie)
    pair[1].wait.assert_called()


def test_producer_hang_after_consumer_exit_is_timeout():
    # P2: the consumer finishes but the producer hangs (ignores SIGPIPE). Must
    # become a classified 'timeout', not a bare TimeoutExpired mis-reported as
    # snapshot_failed -- and both children must be killed.
    pair = _pair(0, 0, _SUMMARY, prod_hang=True)
    with mock.patch("app.backup.subprocess.Popen", side_effect=pair):
        with pytest.raises(backup.BackupError) as exc:
            _call()
    assert exc.value.code == "timeout"
    pair[0].kill.assert_called_once()
    pair[1].kill.assert_called_once()
    pair[1].wait.assert_called()  # consumer reaped in the reap loop


def test_no_secret_in_argv():
    # Secrets reach restic via --env NAME (name-only) + env=, never in the argv
    # (readable via ps). Use a distinctive sentinel password and scan joined argv.
    sentinel = "S3cr3t-Sentinel-Value"
    s = _settings(restic_password=sentinel)
    with mock.patch("app.backup.subprocess.Popen", side_effect=_pair(0, 0, _SUMMARY)) as P:
        backup._dump_and_backup(s, "i", "dbcid", ["pg_dump"], "i/db.sql")
    for call in P.call_args_list:
        assert sentinel not in " ".join(call.args[0])
