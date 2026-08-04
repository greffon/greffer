"""0002 deletes the key material the pre-fix staging code left in the CWD.

Note run() takes data_root per the runner contract even though this migration
ignores it (the strays are in the CWD, not under $GREFFON_PATH). The contract
itself is enforced generically in test_ops_migrations_runner_contract.py --
these unit tests call run() directly and so cannot catch a signature drift,
which is exactly how the boot-fatal version of this migration shipped."""
import os

from apps.utils.ops_migrations.migrations._0002_purge_staged_key_strays import (
    PurgeStagedKeyStrays,
)

_UUID = '002498fe-9d4c-4a38-b3fd-dca8482a5499'
_UUID2 = '11113333-9d4c-4a38-b3fd-dca8482a5499'
_KEY = '-----BEGIN PRIVATE KEY-----\nsecret\n'
_CERT = '-----BEGIN CERTIFICATE-----\ncert\n'


def test_removes_uuid_named_pem_strays(tmp_path, monkeypatch):
    (tmp_path / _UUID).write_text(_KEY)
    (tmp_path / _UUID2).write_text(_CERT)
    monkeypatch.chdir(tmp_path)
    summary = PurgeStagedKeyStrays().run(str(tmp_path / 'data'))
    assert summary['migrated'] == 2
    assert summary['private_keys_removed'] == 1
    assert summary['certificates_removed'] == 1
    assert list(tmp_path.iterdir()) == []


def test_leaves_everything_else_alone(tmp_path, monkeypatch):
    """Narrow by construction: uuid-shaped-but-not-PEM, PEM-but-not-uuid-shaped,
    a directory, and anything with an extension all survive."""
    (tmp_path / _UUID).write_text('{"not": "pem"}')          # uuid, not PEM
    (tmp_path / 'server.key').write_text(_KEY)               # PEM, not uuid
    (tmp_path / f'{_UUID2}.json').write_text(_KEY)           # has an extension
    (tmp_path / _UUID2).mkdir()                              # a directory
    monkeypatch.chdir(tmp_path)
    summary = PurgeStagedKeyStrays().run(str(tmp_path / 'data'))
    assert summary['migrated'] == 0
    assert len(list(tmp_path.iterdir())) == 4


def test_is_idempotent(tmp_path, monkeypatch):
    (tmp_path / _UUID).write_text(_KEY)
    monkeypatch.chdir(tmp_path)
    assert PurgeStagedKeyStrays().run(str(tmp_path / 'data'))['migrated'] == 1
    assert PurgeStagedKeyStrays().run(str(tmp_path / 'data'))['migrated'] == 0


def test_does_not_descend_into_subdirectories(tmp_path, monkeypatch):
    """Only the CWD root — the old code never wrote deeper, and a broad sweep
    over a data root would be far too dangerous a thing to run at boot."""
    sub = tmp_path / 'data'
    sub.mkdir()
    (sub / _UUID).write_text(_KEY)
    monkeypatch.chdir(tmp_path)
    assert PurgeStagedKeyStrays().run(str(tmp_path / 'data'))['migrated'] == 0
    assert (sub / _UUID).exists()


def test_a_file_that_fails_to_unlink_is_not_counted_as_removed(
        tmp_path, monkeypatch):
    """The summary is the operator's signal for whether key material is still
    exposed. Counting at classification time (before the unlink) reported an
    UNDELETED private key as removed -- i.e. it said the leak was cleaned up
    while the key was still on disk."""
    (tmp_path / _UUID).write_text(_KEY)      # this one will fail to unlink
    (tmp_path / _UUID2).write_text(_KEY)     # this one succeeds
    monkeypatch.chdir(tmp_path)

    real_unlink = os.unlink

    def _unlink(path, *a, **kw):
        if os.path.basename(path) == _UUID:
            raise OSError(13, 'Permission denied')
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, 'unlink', _unlink)
    summary = PurgeStagedKeyStrays().run(str(tmp_path / 'data'))

    assert summary['errors'] == 1
    assert summary['migrated'] == 1
    # The key point: exactly ONE key is reported removed, not two.
    assert summary['private_keys_removed'] == 1
    assert (summary['private_keys_removed'] + summary['certificates_removed']
            == summary['migrated'])
    # And the undeleted one really is still there.
    assert (tmp_path / _UUID).exists()


def test_skipped_counts_the_candidates_that_were_left_alone(
        tmp_path, monkeypatch):
    """`skipped` was hardcoded 0, so an operator could not tell the
    narrow-by-construction filters had actually engaged."""
    (tmp_path / _UUID).write_text(_KEY)               # removed
    (tmp_path / _UUID2).write_text('{"not": "pem"}')  # uuid-shaped, not PEM
    (tmp_path / 'server.key').write_text(_KEY)        # PEM, not uuid-shaped
    monkeypatch.chdir(tmp_path)
    summary = PurgeStagedKeyStrays().run(str(tmp_path / 'data'))
    assert summary['migrated'] == 1
    # `skipped` counts uuid-NAMED candidates the content sniff spared -- NOT
    # every entry in the CWD. The CWD is the whole greffer checkout, so counting
    # unrelated files would report ~28 on a clean node and mean nothing.
    assert summary['skipped'] == 1, 'server.key is not a candidate at all'
