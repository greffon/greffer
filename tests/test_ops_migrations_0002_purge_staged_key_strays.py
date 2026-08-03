"""0002 deletes the key material the pre-fix staging code left in the CWD."""
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
    summary = PurgeStagedKeyStrays().run()
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
    summary = PurgeStagedKeyStrays().run()
    assert summary['migrated'] == 0
    assert len(list(tmp_path.iterdir())) == 4


def test_is_idempotent(tmp_path, monkeypatch):
    (tmp_path / _UUID).write_text(_KEY)
    monkeypatch.chdir(tmp_path)
    assert PurgeStagedKeyStrays().run()['migrated'] == 1
    assert PurgeStagedKeyStrays().run()['migrated'] == 0


def test_does_not_descend_into_subdirectories(tmp_path, monkeypatch):
    """Only the CWD root — the old code never wrote deeper, and a broad sweep
    over a data root would be far too dangerous a thing to run at boot."""
    sub = tmp_path / 'data'
    sub.mkdir()
    (sub / _UUID).write_text(_KEY)
    monkeypatch.chdir(tmp_path)
    assert PurgeStagedKeyStrays().run()['migrated'] == 0
    assert (sub / _UUID).exists()
