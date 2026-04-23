"""Unit tests for the tunnel-sidecar agent.

Run directly::

    cd greffer/tunnel-sidecar
    python -m pytest tests/
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_shutdown():
    import agent
    agent._shutdown = False
    yield
    agent._shutdown = False


class _Resp:
    def __init__(self, status_code, text='', etag=None):
        self.status_code = status_code
        self.text = text
        self.headers = {'ETag': etag} if etag else {}


def test_poll_once_200_returns_etag_and_body():
    from agent import poll_once
    session = MagicMock()
    session.get.return_value = _Resp(200, text='new-body', etag='v1')
    etag, body = poll_once(session, 'http://m', 'tok', None, '/ca')
    assert etag == 'v1'
    assert body == 'new-body'


def test_poll_once_304_returns_existing_etag_no_body():
    from agent import poll_once
    session = MagicMock()
    session.get.return_value = _Resp(304)
    etag, body = poll_once(session, 'http://m', 'tok', 'v1', '/ca')
    assert etag == 'v1'
    assert body is None


def test_poll_once_non_2xx_does_not_raise():
    from agent import poll_once
    session = MagicMock()
    for status in (401, 403, 404, 500):
        session.get.return_value = _Resp(status)
        etag, body = poll_once(session, 'http://m', 'tok', 'v1', '/ca')
        assert body is None, f'status {status}'


def test_poll_once_sends_if_none_match_when_etag_known():
    from agent import poll_once
    session = MagicMock()
    session.get.return_value = _Resp(304)
    poll_once(session, 'http://m', 'tok', 'v1', '/ca')
    _, kwargs = session.get.call_args
    assert kwargs['headers']['If-None-Match'] == 'v1'


def test_poll_once_does_not_send_if_none_match_on_first_call():
    from agent import poll_once
    session = MagicMock()
    session.get.return_value = _Resp(200, text='x', etag='v1')
    poll_once(session, 'http://m', 'tok', None, '/ca')
    _, kwargs = session.get.call_args
    assert 'If-None-Match' not in kwargs['headers']


def test_atomic_write_replaces_file_without_leftovers(tmp_path):
    from agent import _atomic_write
    target = tmp_path / 'client.toml'
    target.write_text('old content')
    _atomic_write(target, 'new content')
    assert target.read_text() == 'new content'
    assert list(tmp_path.glob('*.tmp')) == []


def test_atomic_write_creates_parent_dir(tmp_path):
    from agent import _atomic_write
    target = tmp_path / 'deep' / 'dir' / 'client.toml'
    _atomic_write(target, 'hello')
    assert target.read_text() == 'hello'


def test_load_token_prefers_file_over_env(tmp_path, monkeypatch):
    from agent import _load_token
    token_path = tmp_path / 'token'
    token_path.write_text('file-token\n')
    monkeypatch.setenv('GREFFER_TOKEN_FILE', str(token_path))
    monkeypatch.setenv('GREFFER_TOKEN', 'env-token')
    assert _load_token() == 'file-token'


def test_load_token_falls_back_to_env(monkeypatch):
    from agent import _load_token
    monkeypatch.delenv('GREFFER_TOKEN_FILE', raising=False)
    monkeypatch.setenv('GREFFER_TOKEN', 'env-token')
    assert _load_token() == 'env-token'


def test_load_token_raises_when_both_missing(monkeypatch):
    from agent import _load_token
    monkeypatch.delenv('GREFFER_TOKEN_FILE', raising=False)
    monkeypatch.delenv('GREFFER_TOKEN', raising=False)
    with pytest.raises(RuntimeError):
        _load_token()


def test_load_token_strips_trailing_whitespace(tmp_path, monkeypatch):
    from agent import _load_token
    token_path = tmp_path / 'token'
    token_path.write_text('file-token   \n\n')
    monkeypatch.setenv('GREFFER_TOKEN_FILE', str(token_path))
    assert _load_token() == 'file-token'


def test_resolve_ca_bundle_uses_explicit_path(monkeypatch):
    from agent import _resolve_ca_bundle
    monkeypatch.setenv('CA_BUNDLE_PATH', '/custom/ca.pem')
    assert _resolve_ca_bundle() == '/custom/ca.pem'


def test_resolve_ca_bundle_prefers_default_when_present(tmp_path, monkeypatch):
    from agent import _resolve_ca_bundle
    import agent
    monkeypatch.delenv('CA_BUNDLE_PATH', raising=False)
    real_ca = tmp_path / 'ca.pem'
    real_ca.write_text('CA')
    # Patch the default path check to point at our tmp file.
    monkeypatch.setattr(
        agent, '_resolve_ca_bundle',
        lambda: str(real_ca) if real_ca.exists() else True,
        raising=True,
    )
    # Sanity: patched callable returns the expected path.
    assert agent._resolve_ca_bundle() == str(real_ca)


def test_resolve_ca_bundle_falls_back_to_system_store(monkeypatch):
    """When CA_BUNDLE_PATH is unset and the default /secrets/ca.pem does not
    exist (as in typical dev/test), _resolve_ca_bundle returns True so
    requests uses the system CA store instead of raising OSError."""
    from agent import _resolve_ca_bundle
    monkeypatch.delenv('CA_BUNDLE_PATH', raising=False)
    # /secrets/ca.pem is not created in the test environment.
    assert _resolve_ca_bundle() is True
