"""``content`` volume files must never be staged in the CWD, and must not
survive the copy.

The CWD is the bind-mounted greffer checkout (WORKDIR /app + ./:/app), and the
content staged here includes each instance's unencrypted TLS private key. The
original implementation wrote a bare ``uuid4()`` filename there and never
removed it, so every greffon start left a private key on the operator's host
permanently — and outside .gitignore.
"""
import os
from unittest import mock

from apps.utils.docker import volume as volume_mod


def _volume():
    return {
        'value': 'someinstance_nginx_volume',
        'files': [
            {'type': 'content', 'content': 'CERTDATA', 'dest': 'pem.crt'},
            {'type': 'content',
             'content': '-----BEGIN PRIVATE KEY-----\nsecret\n',
             'dest': 'cert.key'},
        ],
    }


def test_content_is_not_staged_in_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # stand in for the bind-mounted /app
    copied = []
    with mock.patch.object(volume_mod.subprocess, 'run',
                           side_effect=lambda argv, **kw: copied.append(argv)):
        volume_mod.docker_copy_file_into_volume(_volume())

    assert list(tmp_path.iterdir()) == [], (
        'staged volume content was written into the working directory — on a '
        'real greffer that is the operator checkout, and one of these files is '
        'an unencrypted TLS private key'
    )
    # Sanity: the copies really were attempted, so the assertion above is not
    # passing merely because nothing ran.
    assert sum(1 for argv in copied if argv[:2] == ['docker', 'cp']) == 2


def test_staged_files_are_removed_after_the_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []

    def _run(argv, **kw):
        if argv[:2] == ['docker', 'cp']:
            seen.append(argv[2])          # the staged path, while it still exists
            assert os.path.exists(argv[2]), 'staged file missing during copy'
    with mock.patch.object(volume_mod.subprocess, 'run', side_effect=_run):
        volume_mod.docker_copy_file_into_volume(_volume())

    assert seen, 'no copy was attempted'
    for path in seen:
        assert not os.path.exists(path), f'staged key material survived: {path}'


def test_staged_files_are_removed_even_when_the_copy_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []

    def _run(argv, **kw):
        if argv[:2] == ['docker', 'cp']:
            seen.append(argv[2])
            raise RuntimeError('docker cp exploded')
    with mock.patch.object(volume_mod.subprocess, 'run', side_effect=_run):
        try:
            volume_mod.docker_copy_file_into_volume(_volume())
        except RuntimeError:
            pass

    assert seen, 'no copy was attempted'
    for path in seen:
        assert not os.path.exists(path), (
            f'key material survived a failed copy: {path}')
