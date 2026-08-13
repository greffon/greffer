"""The operator-facing surface: the endpoint, the CLI and the worker loop.

None of this had a single executing test, and it is ~150 lines of exactly the
code an operator reaches for during an incident. A review round demonstrated
that the 429 mapping added specifically to stop the CLI reporting a phantom
success could be deleted with the whole suite still green, as could the
mod-256 exit-code fix from the same commit.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.auth import TOKEN_HEADER
from app.main import create_app
from app.settings import Settings
from app.workers import cert_renewal

RENEW_URL = '/api/controller/renew-certs/'


@pytest.fixture
def client(settings: Settings):
    app = create_app(token='tok', settings=settings)
    with TestClient(app) as c:
        yield c


def _result(**kw):
    base = {'considered': 1, 'renewed': 1, 'skipped': 0, 'errors': 0,
            'selected': 'renewed'}
    base.update(kw)
    return cert_renewal.PassResult(**base)


def test_the_endpoint_requires_the_greffer_token(client) -> None:
    assert client.post(RENEW_URL).status_code == 401


def test_a_capped_node_is_a_429_not_a_clean_pass(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The single most important mapping here.

    Returning 200 {"errors": 0} tells an operator staring at a 502 that the
    renewal happened. It did not: the pass stopped at the manager's hourly cap.
    """
    monkeypatch.setattr(
        'app.workers.cert_renewal.renew_all',
        lambda *a, **k: (_ for _ in ()).throw(cert_renewal.NodeCapped()))

    res = client.post(RENEW_URL, headers={TOKEN_HEADER: 'tok'})

    assert res.status_code == 429
    assert 'node_rate_limited' in res.text


def test_a_concurrent_pass_is_a_409(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'app.workers.cert_renewal.renew_all',
        lambda *a, **k: (_ for _ in ()).throw(cert_renewal.RenewalAlreadyRunning()))

    res = client.post(RENEW_URL, headers={TOKEN_HEADER: 'tok'})

    assert res.status_code == 409
    assert 'renewal_in_progress' in res.text


def test_an_unknown_instance_is_a_404_not_a_500(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        'app.workers.cert_renewal.renew_all',
        lambda *a, **k: (_ for _ in ()).throw(cert_renewal.InstanceNotFound('x')))

    res = client.post(RENEW_URL, params={'id': 'x'}, headers={TOKEN_HEADER: 'tok'})

    assert res.status_code == 404


def test_a_fleet_wide_force_is_refused_server_side(client) -> None:
    """The manager holds this token too, so a CLI-only guard is no guard."""
    res = client.post(RENEW_URL, params={'force': 'true'},
                      headers={TOKEN_HEADER: 'tok'})

    assert res.status_code == 400
    assert 'force_requires_instance' in res.text


def test_a_skipped_target_is_not_reported_as_done(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's sequence: restart the app, still 502, force a renewal.

    Inside the compose settle window that instance is SKIPPED -- legitimately,
    but nothing was renewed, and errors=0 would say otherwise.
    """
    monkeypatch.setattr('app.workers.cert_renewal.renew_all',
                        lambda *a, **k: _result(renewed=0, skipped=1, selected='skipped'))

    res = client.post(RENEW_URL, params={'id': 'i1', 'force': 'true'},
                      headers={TOKEN_HEADER: 'tok'})

    assert res.status_code == 409
    assert 'instance_not_renewed' in res.text


def test_a_real_renewal_is_a_200_with_counts(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('app.workers.cert_renewal.renew_all',
                        lambda *a, **k: _result(considered=3, renewed=2, skipped=1))

    res = client.post(RENEW_URL, headers={TOKEN_HEADER: 'tok'})

    assert res.status_code == 200
    assert res.json() == {'errors': 0, 'renewed': 2, 'skipped': 1, 'considered': 3}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, code, text='{}', payload=None):
        self.status_code = code
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _run_cli(monkeypatch, settings, response, argv):
    from app import cli

    monkeypatch.setattr(cli, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.token.load_persisted_token', lambda p: 'tok')
    seen: dict = {}

    def _post(url, **kw):
        seen['url'] = url
        seen.update(kw)
        return response

    monkeypatch.setattr('requests.post', _post)
    return cli.main(argv), seen


def test_the_cli_exits_nonzero_when_the_node_is_capped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run_cli(monkeypatch, settings,
                       _Res(429, '{"detail": "node_rate_limited"}'),
                       ['renew_certs', '--instance', 'i1', '--force'])
    assert code != 0


def test_the_cli_exits_nonzero_when_the_target_was_not_renewed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run_cli(monkeypatch, settings,
                       _Res(409, '{"detail": "instance_not_renewed:skipped"}'),
                       ['renew_certs', '--instance', 'i1'])
    assert code != 0


def test_the_cli_exit_code_does_not_truncate(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.exit truncates modulo 256, so returning a raw count of 256 failing
    instances reported total success to any wrapping script."""
    code, _ = _run_cli(monkeypatch, settings,
                       _Res(200, '{}', {'errors': 256}), ['renew_certs'])
    assert code == 1


def test_the_cli_succeeds_on_a_clean_pass(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    code, seen = _run_cli(monkeypatch, settings,
                          _Res(200, '{}', {'errors': 0, 'renewed': 2}),
                          ['renew_certs'])
    assert code == 0
    assert seen['url'].startswith('http://127.0.0.1:8000/')
    assert seen['headers'][TOKEN_HEADER] == 'tok'


def test_the_cli_refuses_a_fleet_wide_force(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    code, seen = _run_cli(monkeypatch, settings, _Res(200), ['renew_certs', '--force'])
    assert code != 0
    assert 'url' not in seen, 'must not even reach the node'


def test_the_cli_never_mints_a_token(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """resolve_token MINTS and PERSISTS when no token exists.

    Doing that here would 401 against the running greffer and stage a
    CLI-invented token as the node's identity at the next re-register.
    """
    from app import cli

    settings.greffer_token = ''
    settings.greffon_path = tmp_path
    monkeypatch.setattr(cli, 'get_settings', lambda: settings)
    monkeypatch.setattr('requests.post',
                        lambda *a, **k: pytest.fail('must not call the node'))

    assert cli.main(['renew_certs']) != 0
    assert not (tmp_path / '.greffer-token').exists(), 'the CLI minted a token'


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_loop_sleeps_before_the_first_tick(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting the sleep turns the worker into a hot loop hammering the mint
    endpoint, which the suite could not see."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    slept: list = []
    ticks: list = []

    async def _sleep(n):
        slept.append(n)
        raise asyncio.CancelledError

    def _renew(*a, **kw):
        # Bounded: without a sleep the loop spins, and a test that hangs is a
        # CI timeout rather than a failure anyone can read.
        ticks.append(1)
        if len(ticks) > 5:
            raise asyncio.CancelledError
        return _result()

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _renew)
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept == [settings.greffer_cert_renewal_interval]
    assert ticks == [], 'the loop must sleep BEFORE its first tick'


@pytest.mark.asyncio
async def test_a_capped_tick_retries_within_the_hour(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap refills hourly; waiting a full interval spends a sixth of the
    node's budget while certificates expire."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    ticks: list = []

    def _capped(*a, **kw):
        ticks.append(1)
        if len(ticks) > 5:
            raise asyncio.CancelledError
        raise cert_renewal.NodeCapped

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _capped)
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept[1] == cert_renewal._CAPPED_RETRY_SECONDS


@pytest.mark.asyncio
async def test_an_operator_pass_does_not_look_like_a_crashed_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    calls: list = []

    async def _sleep(n):
        calls.append(n)
        if len(calls) >= 2:
            raise asyncio.CancelledError

    ticks: list = []

    def _busy(*a, **kw):
        ticks.append(1)
        if len(ticks) > 5:
            raise asyncio.CancelledError
        raise cert_renewal.RenewalAlreadyRunning

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _busy)
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert 'cert_renewal_tick_failed' not in caplog.text


@pytest.mark.asyncio
async def test_the_tick_reads_the_token_fresh_each_time(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """register.py rewrites app.state.greffer_token on rotation.

    Snapshotting it at startup 403s every mint and report after any rotation,
    and each instance then walks the backoff to the cap: silent node-wide
    certificate expiry. monitor.py and heartbeat.py both re-read per tick.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'token-A'
    seen: list = []
    ticks: list = []

    async def _sleep(n):
        ticks.append(n)
        if len(ticks) >= 3:
            raise asyncio.CancelledError

    def _renew(s, token, **kw):
        seen.append(token)
        if len(seen) > 5:
            raise asyncio.CancelledError
        app.state.greffer_token = 'token-B'
        return _result()

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _renew)
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert seen == ['token-A', 'token-B'], seen


def test_renew_all_forwards_force_to_each_instance(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag can otherwise be dropped anywhere between argparse and the
    per-instance guards with CI green, and the symptom is a silent no-op."""
    seen: list = []
    monkeypatch.setattr(
        cert_renewal, 'renew_one',
        lambda s, t, g, force=False: seen.append(force) or cert_renewal.Outcome(
            cert_renewal.RENEWED, 'aa'))
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'i1': 'running'}):
        cert_renewal.renew_all(settings, 'tok', only='i1', force=True)

    assert seen == [True]
