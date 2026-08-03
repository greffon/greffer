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


def test_path_sources_are_never_deleted(tmp_path, monkeypatch):
    """``type: 'path'`` sources are pre-existing operator files — the rendered
    nginx.conf and every config JSON under $GREFFON_PATH. They are NOT staged
    and must never be unlinked. Without this, moving `staged.append` out of the
    content branch would still pass every other test in this file while the
    greffer deleted the operator's configs."""
    src = tmp_path / 'nginx.conf'
    src.write_text('server {}')
    monkeypatch.chdir(tmp_path)
    with mock.patch.object(volume_mod.subprocess, 'run'):
        volume_mod.docker_copy_file_into_volume({
            'value': 'v',
            'files': [{'type': 'path', 'src': str(src), 'dest': 'nginx.conf'}],
        })
    assert src.exists(), 'a pre-existing path source was deleted'


def test_earlier_staged_files_are_cleaned_when_a_later_copy_raises(
        tmp_path, monkeypatch):
    """The raise must land on the SECOND file, so this exercises the
    accumulating-`staged`-list property. Raising on the first would pass even
    if cleanup only ever handled one entry."""
    monkeypatch.chdir(tmp_path)
    seen = []

    def _run(argv, **kw):
        if argv[:2] == ['docker', 'cp']:
            seen.append(argv[2])
            if len(seen) == 2:
                raise RuntimeError('second copy exploded')
    with mock.patch.object(volume_mod.subprocess, 'run', side_effect=_run):
        try:
            volume_mod.docker_copy_file_into_volume(_volume())
        except RuntimeError:
            pass

    assert len(seen) == 2, 'the raise did not land on the second copy'
    for path in seen:
        assert not os.path.exists(path), f'staged key material survived: {path}'
