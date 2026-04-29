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
    agent._shutdown_event.clear()
    yield
    agent._shutdown_event.clear()


class _Resp:
    def __init__(self, status_code, text='', etag=None):
        self.status_code = status_code
        self.text = text
        self.headers = {'ETag': etag} if etag else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _r
            raise _r.HTTPError(
                f'{self.status_code} from server', response=self,
            )


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


def test_poll_once_4xx_does_not_raise():
    """4xx: log and keep polling at base interval — typically operator-fixable
    state (bad token, greffer not provisioned, mode masked as 404)."""
    from agent import poll_once
    session = MagicMock()
    for status in (401, 403, 404):
        session.get.return_value = _Resp(status)
        etag, body = poll_once(session, 'http://m', 'tok', 'v1', '/ca')
        assert body is None, f'status {status}'


def test_poll_once_5xx_raises_for_backoff():
    """5xx: re-raise so main loop's RequestException branch applies
    exponential backoff during a manager outage, instead of every sidecar
    re-polling at the base interval and amplifying load."""
    import requests
    from agent import poll_once
    session = MagicMock()
    for status in (500, 502, 503, 504):
        session.get.return_value = _Resp(status)
        with pytest.raises(requests.HTTPError):
            poll_once(session, 'http://m', 'tok', 'v1', '/ca')


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


def test_load_token_rejects_empty_file(tmp_path, monkeypatch):
    """Blank or whitespace-only secret file would let the agent loop on
    401 forever with no clear diagnostic. Fail fast at startup instead."""
    from agent import _load_token
    token_path = tmp_path / 'token'
    token_path.write_text('   \n\n')
    monkeypatch.setenv('GREFFER_TOKEN_FILE', str(token_path))
    with pytest.raises(RuntimeError, match='empty'):
        _load_token()


def test_load_token_translates_missing_file_to_runtime_error(
    tmp_path, monkeypatch,
):
    """An unreadable file would crash main() with an unhandled OSError;
    translating to RuntimeError lets the existing startup-config-error
    path run and exit cleanly."""
    from agent import _load_token
    monkeypatch.setenv(
        'GREFFER_TOKEN_FILE', str(tmp_path / 'does-not-exist'),
    )
    with pytest.raises(RuntimeError, match='cannot read'):
        _load_token()


def test_resolve_poll_interval_default_when_unset(monkeypatch):
    from agent import _resolve_poll_interval, DEFAULT_POLL_INTERVAL
    monkeypatch.delenv('POLL_INTERVAL_SECONDS', raising=False)
    assert _resolve_poll_interval() == DEFAULT_POLL_INTERVAL


def test_resolve_poll_interval_uses_explicit_value(monkeypatch):
    from agent import _resolve_poll_interval
    monkeypatch.setenv('POLL_INTERVAL_SECONDS', '5')
    assert _resolve_poll_interval() == 5.0


def test_resolve_poll_interval_rejects_non_numeric(monkeypatch):
    from agent import _resolve_poll_interval
    monkeypatch.setenv('POLL_INTERVAL_SECONDS', 'fast')
    with pytest.raises(RuntimeError, match='must be numeric'):
        _resolve_poll_interval()


def test_resolve_poll_interval_rejects_zero(monkeypatch):
    from agent import _resolve_poll_interval
    monkeypatch.setenv('POLL_INTERVAL_SECONDS', '0')
    with pytest.raises(RuntimeError, match='must be > 0'):
        _resolve_poll_interval()


def test_resolve_poll_interval_rejects_negative(monkeypatch):
    from agent import _resolve_poll_interval
    monkeypatch.setenv('POLL_INTERVAL_SECONDS', '-1')
    with pytest.raises(RuntimeError, match='must be > 0'):
        _resolve_poll_interval()


def test_resolve_ca_bundle_uses_explicit_path(monkeypatch):
    from agent import _resolve_ca_bundle
    monkeypatch.setenv('CA_BUNDLE_PATH', '/custom/ca.pem')
    assert _resolve_ca_bundle() == '/custom/ca.pem'


def test_resolve_ca_bundle_prefers_default_when_present(tmp_path, monkeypatch):
    """Exercise the real _resolve_ca_bundle implementation: if CA_BUNDLE_PATH
    is unset and DEFAULT_CA_BUNDLE_PATH points at an existing file, return
    that path (not True). Patches the module-level default so the real
    function branch runs against a tmp file."""
    import agent
    monkeypatch.delenv('CA_BUNDLE_PATH', raising=False)
    real_ca = tmp_path / 'ca.pem'
    real_ca.write_text('CA')
    monkeypatch.setattr(agent, 'DEFAULT_CA_BUNDLE_PATH', str(real_ca))
    assert agent._resolve_ca_bundle() == str(real_ca)


def test_resolve_ca_bundle_falls_back_to_system_store(tmp_path, monkeypatch):
    """When CA_BUNDLE_PATH is unset and DEFAULT_CA_BUNDLE_PATH does not exist,
    _resolve_ca_bundle returns True so requests uses the system CA store
    instead of raising OSError on a missing file."""
    import agent
    monkeypatch.delenv('CA_BUNDLE_PATH', raising=False)
    # Point the default at a path we know does not exist.
    monkeypatch.setattr(
        agent, 'DEFAULT_CA_BUNDLE_PATH', str(tmp_path / 'not-here.pem'),
    )
    assert agent._resolve_ca_bundle() is True
