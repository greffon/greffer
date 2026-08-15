"""The operator-facing surface: the endpoint, the CLI and the worker loop.

None of this had a single executing test, and it is ~150 lines of exactly the
code an operator reaches for during an incident. A review round demonstrated
that the 429 mapping added specifically to stop the CLI reporting a phantom
success could be deleted with the whole suite still green, as could the
mod-256 exit-code fix from the same commit.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.auth import TOKEN_HEADER
from app.main import create_app
from app.settings import Settings
from app.workers import cert_renewal

RENEW_URL = '/api/controller/renew-certs/'


@pytest.fixture(autouse=True)
def _no_carried_state():
    """Module-level state leaks between tests otherwise.

    A debt written by one test satisfied another's `'i1' in _unconfirmed`
    assertion on its own, which is a test that passes without the code under
    it doing anything.
    """
    cert_renewal._unconfirmed.clear()
    cert_renewal._renewal_backoff.clear()
    cert_renewal._stop.clear()
    yield
    cert_renewal._unconfirmed.clear()
    cert_renewal._renewal_backoff.clear()
    cert_renewal._stop.clear()


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
    app.state.registered = asyncio.Event()
    app.state.registered.set()
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

    assert ticks == [], 'the loop must sleep BEFORE its first tick'
    # Bounded by the jitter window, NOT the full interval. A greffer restart
    # leaves sidecars serving whatever they had, so a six-hour first sleep
    # would outlive the expiry it is meant to catch -- on the very restart
    # that delivers this feature to the node.
    assert len(slept) == 1
    assert 0 <= slept[0] <= cert_renewal._STARTUP_JITTER_SECONDS
    assert slept[0] < settings.greffer_cert_renewal_interval


@pytest.mark.asyncio
async def test_only_the_first_sleep_is_shortened(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The jitter is a startup concession, not the new cadence.

    Shortening every sleep would turn a 6-hour cadence into a 5-minute one
    and walk the whole fleet into the manager's per-greffer mint cap.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', lambda *a, **kw: _result())
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept[0] <= cert_renewal._STARTUP_JITTER_SECONDS
    assert slept[1] == settings.greffer_cert_renewal_interval


@pytest.mark.asyncio
async def test_a_capped_tick_retries_within_the_hour(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap refills hourly; waiting a full interval spends a sixth of the
    node's budget while certificates expire."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 3:
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

    # The whole SEQUENCE, not just that the short delay appears somewhere.
    # Asserting slept[1] alone passed while the loop then slept another full
    # interval on top, making the real retry 7h and the log line a lie.
    assert slept[0] <= cert_renewal._STARTUP_JITTER_SECONDS, slept
    assert slept[1:3] == [
        cert_renewal._CAPPED_RETRY_SECONDS,
        cert_renewal._CAPPED_RETRY_SECONDS,
    ], slept


@pytest.mark.asyncio
async def test_an_operator_pass_does_not_look_like_a_crashed_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
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
def test_an_unauthorized_report_keeps_the_debt(settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token rotated between this pass reading app.state and the manager
    receiving the call. The next pass reads a fresh token, so dropping the debt
    here strands a serving certificate the manager never learns about."""
    settings.greffon_path = tmp_path
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)
    monkeypatch.setattr(
        'requests.post',
        lambda *a, **k: type('R', (), {'status_code': 403, 'text': ''})())

    cert_renewal._report(settings, 'stale-token', 'i1', 'aa11')

    assert cert_renewal._unconfirmed.get('i1') == 'aa11'
def test_the_ledger_is_written_atomically(settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rename-into-place, because the event this ledger exists to survive IS a kill.

    write_text truncates and then writes, so a kill or ENOSPC inside that
    window leaves a truncated file that reads back as ZERO debts -- losing the
    debts rather than just the update. It is rewritten once per failed report,
    so a manager outage across a fleet opens one such window per instance.

    Asserting the rename rather than a corrupted-file outcome: the truncation
    window cannot be observed from a test, and an assertion that merely reads
    the file back afterwards passes for the unsafe implementation too.
    """
    import os as _os

    settings.greffon_path = tmp_path
    ledger = tmp_path / '.cert-unconfirmed.json'
    replaced: list = []
    real_replace = _os.replace
    monkeypatch.setattr(_os, 'replace',
                        lambda a, b: replaced.append((str(a), str(b))) or real_replace(a, b))

    cert_renewal._unconfirmed['i1'] = 'aa11'
    cert_renewal._save_unconfirmed(settings)

    assert replaced, 'the ledger was written in place, not renamed into place'
    src, dst = replaced[-1]
    assert dst == str(ledger)
    assert src != str(ledger), 'the temp file must be a different path'
    assert 'i1' in ledger.read_text()
    assert not list(tmp_path.glob('*.tmp')), 'temp file left behind'


def test_a_failed_rename_leaves_the_existing_ledger_intact(
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as _os

    settings.greffon_path = tmp_path
    ledger = tmp_path / '.cert-unconfirmed.json'
    cert_renewal._unconfirmed['keep-me'] = 'aa11'
    cert_renewal._save_unconfirmed(settings)

    monkeypatch.setattr(_os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('ENOSPC')))
    cert_renewal._unconfirmed['and-me'] = 'bb22'
    cert_renewal._save_unconfirmed(settings)  # must not raise

    import json as _json
    assert _json.loads(ledger.read_text()) == {'keep-me': 'aa11'}


@pytest.mark.asyncio
async def test_a_short_interval_clamps_the_capped_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node on a 30-minute tick must not wait an HOUR after being rate
    limited -- that is longer than it would have waited anyway, the inverse of
    what the capped retry is for."""
    from fastapi import FastAPI

    settings.greffer_cert_renewal_interval = 600
    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(
        cert_renewal, 'renew_all',
        lambda *a, **k: (_ for _ in ()).throw(cert_renewal.NodeCapped()))
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept[0] <= cert_renewal._STARTUP_JITTER_SECONDS, slept
    # The clamp is the point: _CAPPED_RETRY_SECONDS (3600) must not lengthen
    # a 600s interval.
    assert slept[1:] == [600], slept


@pytest.mark.asyncio
async def test_the_delay_returns_to_normal_after_a_capped_tick(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap ONCE, then recover.

    Every other test here stubs renew_all to cap on every tick, so
    [interval, capped, capped] comes out identically whether or not the delay
    is reset -- the reset was entirely unpinned. Without it a single
    rate-limited tick would pin the node to the short retry forever, hammering
    the mint endpoint six times more often than intended for good.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []
    ticks: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 3:
            raise asyncio.CancelledError

    def _cap_once(*a, **kw):
        ticks.append(1)
        if len(ticks) == 1:
            raise cert_renewal.NodeCapped
        return _result()

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _cap_once)
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    interval = settings.greffer_cert_renewal_interval
    assert slept[0] <= cert_renewal._STARTUP_JITTER_SECONDS, slept
    assert slept[1:] == [cert_renewal._CAPPED_RETRY_SECONDS, interval], slept
def test_the_renewal_worker_is_watched_as_fatal() -> None:
    """A renewal worker that dies silently IS the bug this feature fixes.

    Without it in FATAL_WORKERS, /readyz stays healthy, the watchdog never
    restarts the process, and certificates simply stop being renewed until
    instances 502 -- with the node reporting itself fine throughout.
    """
    from app import readiness

    assert 'greffer-cert-renewal' in readiness.FATAL_WORKERS


@pytest.mark.asyncio
async def test_a_disabled_node_does_not_register_a_dead_worker(settings: Settings) -> None:
    """The task is fatal-watched, so one that returned because the feature is
    off would read as a dead worker and fail /readyz on every such node."""
    from app import workers

    settings.greffer_cert_renewal_enabled = False
    app = create_app(token='tok', settings=settings)
    app.state.registered = asyncio.Event()
    tasks = workers.start_workers(app)
    try:
        names = {t.get_name() for t in tasks}
        assert 'greffer-cert-renewal' not in names
    finally:
        for t in tasks:
            t.cancel()


@pytest.mark.asyncio
async def test_a_pass_waits_for_registration(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick during initial acceptance or a re-registration 403s on every
    request, and _mint reads 403 as the instance's own failure -- so one badly
    timed tick backs off every due instance on the node for 6 to 24 hours."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()  # deliberately NOT set
    ran: list = []

    async def _sleep(n):
        return None

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all',
                        lambda *a, **k: ran.append(1) or _result())

    task = asyncio.create_task(cert_renewal.cert_renewal_worker(app))
    await asyncio.wait({task}, timeout=0.2)
    try:
        assert ran == [], 'a pass ran before the greffer was registered'
        app.state.registered.set()
        await asyncio.wait({task}, timeout=0.2)
        assert ran, 'the pass never ran after registration'
    finally:
        task.cancel()


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
    app.state.registered = asyncio.Event()
    app.state.registered.set()
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


def test_the_owed_ledger_survives_a_greffer_restart(settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The window this covers is a manager outage, and the operator's response
    to one is often to restart or update the greffer -- dropping exactly the
    debts accumulated during it. `greffer update` recreates the process by
    design.
    """
    settings.greffon_path = tmp_path
    monkeypatch.setattr(cert_renewal, 'renew_one',
                        lambda s, t, g, force=False: cert_renewal.Outcome(
                            cert_renewal.NOT_DUE, 'aa'))
    cert_renewal._unconfirmed['i1'] = 'deadbeef'
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'i1': 'running'}):
        cert_renewal.renew_all(settings, 'tok')

    # A fresh process: the in-memory map is gone, the file is not.
    cert_renewal._unconfirmed.clear()
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'i1': 'running'}):
        cert_renewal.renew_all(settings, 'tok')

    assert cert_renewal._unconfirmed.get('i1') == 'deadbeef'


def test_a_corrupt_ledger_does_not_fail_the_pass(settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale bookkeeping is the cost of losing it; a broken pass is not."""
    settings.greffon_path = tmp_path
    (tmp_path / '.cert-unconfirmed.json').write_text('{not json')
    monkeypatch.setattr(cert_renewal, 'renew_one',
                        lambda s, t, g, force=False: cert_renewal.Outcome(
                            cert_renewal.NOT_DUE, 'aa'))
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'i1': 'running'}):
        assert cert_renewal.renew_all(settings, 'tok').errors == 0


def test_an_unwritable_volume_does_not_fail_the_pass(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.greffon_path = pathlib.Path('/nonexistent-volume-xyz')
    monkeypatch.setattr(cert_renewal, 'renew_one',
                        lambda s, t, g, force=False: cert_renewal.Outcome(
                            cert_renewal.RENEWED, 'aa'))
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'i1': 'running'}):
        assert cert_renewal.renew_all(settings, 'tok').renewed == 1


# ---------------------------------------------------------------------------
# The compose in-flight marker. A StartedAt timestamp cannot see this window:
# while compose is PULLING, the existing sidecar is still up with an old start
# time, so it reads as settled right until it is replaced.
# ---------------------------------------------------------------------------


def test_an_unobservable_sidecar_is_still_owed(settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`if served_serial:` skipped the debt for exactly the case that most
    needs chasing."""
    settings.greffon_path = tmp_path
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)
    monkeypatch.setattr(
        'requests.post',
        lambda *a, **k: type('R', (), {'status_code': 503, 'text': ''})())

    cert_renewal._report(settings, 'tok', 'i1', None)

    assert 'i1' in cert_renewal._unconfirmed
def test_a_cancelled_worker_stops_the_pass_between_instances(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """anyio's worker threads are NOT daemons.

    `abandon_on_cancel` stops the loop WAITING on a pass without stopping the
    pass, and the interpreter joins it at exit. The watchdog self-heals by
    SIGTERMing its own process, so a pass grinding through a hung docker daemon
    (60s per call, per instance) would keep the container alive for exactly as
    long as the fault the watchdog exists to recover from.
    """
    seen: list = []

    def _one(s, t, g, force=False):
        seen.append(g)
        cert_renewal._stop.set()  # shutdown begins mid-pass
        return cert_renewal.Outcome(cert_renewal.RENEWED, 'aa')

    monkeypatch.setattr(cert_renewal, 'renew_one', _one)
    fleet = {f'i{n}': 'running' for n in range(10)}
    try:
        with patch('app.workers.status_collect.collect_status_map', return_value=fleet):
            cert_renewal.renew_all(settings, 'tok')
    finally:
        cert_renewal._stop.clear()

    assert len(seen) == 1, (
        f'the pass ground through {len(seen)} instances after shutdown began')


# ---------------------------------------------------------------------------
# Renewal stands off from a live compose child. Deliberately NOT via the
# per-instance lock: the manager chains control ops (the migration cutover runs
# stop then /backup/, start then /restore/) and they all take that lock, so
# holding it across the child 409s the next step and fails the migration.
# ---------------------------------------------------------------------------

class _StillWorking:
    pid = 4242

    def __init__(self, ev=None):
        self._ev = ev

    def wait(self, timeout=None):
        import subprocess
        if self._ev is not None and self._ev.wait(timeout):
            return 0
        raise subprocess.TimeoutExpired('docker-compose', timeout or 0)


def _post_start(client, instance_id, proc):
    from tests.helpers import SAMPLE_START_PAYLOAD

    payload = dict(SAMPLE_START_PAYLOAD)
    payload['id'] = instance_id
    with patch('app.routers.controller.repository') as repo, \
            patch('app.routers.controller.compose') as compose, \
            patch('app.routers.controller.conf'):
        repo.get_compose_file_from_repository.return_value = {}
        repo.get_greffon_info.return_value = {'ports': [], 'id': instance_id}
        compose.start.return_value = proc
        return client.post('/api/controller/start/', json=payload,
                           headers={TOKEN_HEADER: 'tok'})


def test_a_start_marks_the_instance_until_compose_finishes(client) -> None:
    import threading
    import time as _time

    from app.routers import controller

    finished = threading.Event()
    try:
        res = _post_start(client, 'start-me', _StillWorking(finished))
        assert res.status_code == 200, res.text
        assert controller.compose_inflight('start-me') is True, (
            'renewal would write into a sidecar still being recreated')
    finally:
        finished.set()
        for _ in range(200):
            if not controller.compose_inflight('start-me'):
                break
            _time.sleep(0.02)
    assert controller.compose_inflight('start-me') is False, 'never cleared'


def test_a_stop_marks_the_instance_too(client) -> None:
    """The manager restarts an instance as stop-then-start, so a stop that did
    not mark was half the race."""
    import threading
    import time as _time

    from app.routers import controller

    finished = threading.Event()
    try:
        with patch('app.routers.controller.repository') as repo, \
                patch('app.routers.controller.compose') as compose:
            repo.get_greffon_info.return_value = {'ports': [], 'id': 'stop-me'}
            compose.stop.return_value = _StillWorking(finished)
            res = client.post('/api/controller/stop/', json={'id': 'stop-me'},
                              headers={TOKEN_HEADER: 'tok'})
        assert res.status_code == 200, res.text
        assert controller.compose_inflight('stop-me') is True
    finally:
        finished.set()
        for _ in range(200):
            if not controller.compose_inflight('stop-me'):
                break
            _time.sleep(0.02)


def test_a_start_does_not_hold_the_instance_lock_past_its_response(client) -> None:
    """The migration cutover chains stop -> /backup/ and start -> /restore/,
    and both take this lock. Holding it across the compose child 409s the very
    next step and fails every migration.
    """
    import threading

    from app.backup import _instance_lock

    finished = threading.Event()
    try:
        res = _post_start(client, 'chain-me', _StillWorking(finished))
        assert res.status_code == 200, res.text
        lock = _instance_lock('chain-me')
        assert lock.acquire(blocking=False), (
            'a chained /backup/ or /restore/ would get 409 instance_busy here')
        lock.release()
    finally:
        finished.set()


def test_two_children_both_have_to_finish(client) -> None:
    """Counted, not flagged: a stop and a start can overlap, and whichever
    finished first must not un-mark the instance for the other."""
    import threading
    import time as _time

    from app.routers import controller

    quick, slow = threading.Event(), threading.Event()
    try:
        controller.track_compose_child('two', _StillWorking(slow))
        controller.track_compose_child('two', _StillWorking(quick))
        quick.set()
        _time.sleep(0.2)
        assert controller.compose_inflight('two') is True, (
            'the first child to exit unmarked an instance the other is using')
    finally:
        slow.set()
        for _ in range(200):
            if not controller.compose_inflight('two'):
                break
            _time.sleep(0.02)


@pytest.mark.asyncio
async def test_the_stop_flag_is_cleared_before_the_pass_is_scheduled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing it INSIDE the pass erased a cancellation that arrived between
    scheduling and the thread starting, and the pass then ran to completion
    with the event loop already gone."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    observed: list = []

    async def _sleep(n):
        return None

    def _renew(*a, **kw):
        observed.append(cert_renewal._stop.is_set())
        raise asyncio.CancelledError

    cert_renewal._stop.set()  # a stale flag from a previous shutdown
    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _renew)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cert_renewal.cert_renewal_worker(app)
    finally:
        cert_renewal._stop.clear()

    assert observed == [False], 'the pass started with a stale stop flag set'


def test_a_rejected_token_stops_the_pass(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token rotated mid-pass is node-wide, never the instance's fault.

    Charging it to each instance backed off every remaining due one for a fault
    none of them had, and they stay skipped long after the next pass picks up
    the fresh token.
    """
    seen: list = []

    def _one(s, t, g, force=False):
        seen.append(g)
        raise cert_renewal.NodeAuthLost

    monkeypatch.setattr(cert_renewal, 'renew_one', _one)
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'a': 'running', 'b': 'running', 'c': 'running'}), \
            pytest.raises(cert_renewal.NodeAuthLost):
        cert_renewal.renew_all(settings, 'stale-token')

    assert seen == ['a'], 'the pass kept presenting a token the manager rejected'
    assert cert_renewal._renewal_backoff == {}, 'instances penalised for a node fault'


def test_a_rejected_token_is_not_a_clean_pass_at_the_surface(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's emergency lever must not print success.

    Returning normally had the CLI print "renewal pass complete, errors=0" and
    exit 0 after the manager rejected the node's token -- the same failure the
    NodeCapped 429 mapping beside it exists to prevent.
    """
    monkeypatch.setattr(
        'app.workers.cert_renewal.renew_all',
        lambda *a, **k: (_ for _ in ()).throw(cert_renewal.NodeAuthLost()))

    res = client.post(RENEW_URL, headers={TOKEN_HEADER: 'tok'})

    assert res.status_code != 200, res.text
    assert 'node_auth_lost' in res.text


def test_the_cli_exits_nonzero_when_the_token_was_rejected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run_cli(monkeypatch, settings,
                       _Res(502, '{"detail": "node_auth_lost"}'),
                       ['renew_certs'])
    assert code != 0


def test_a_departed_instance_is_dropped_from_the_owed_ledger(
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its twin for the backoff map is pinned; this one was not.

    A decommissioned or migrated-away instance kept its debt in memory AND in
    .cert-unconfirmed.json for the life of the process, so `owed=` in the tick
    diag drifts upward forever on instances that do not exist -- the same drift
    the backoff prune exists to prevent.
    """
    settings.greffon_path = tmp_path
    monkeypatch.setattr(cert_renewal, 'renew_one',
                        lambda s, t, g, force=False: cert_renewal.Outcome(
                            cert_renewal.NOT_DUE, 'aa'))
    cert_renewal._unconfirmed['gone'] = 'dead1'
    cert_renewal._unconfirmed['here'] = 'live1'

    with patch('app.workers.status_collect.collect_status_map',
               return_value={'here': 'running'}):
        cert_renewal.renew_all(settings, 'tok')

    assert set(cert_renewal._unconfirmed) == {'here'}
    assert 'gone' not in (tmp_path / '.cert-unconfirmed.json').read_text()


def test_a_failed_reaper_thread_does_not_unmark_a_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Popping instead of decrementing erased the mark of a child that is
    still running -- the exact failure the counter exists to stop."""
    import threading

    from app.routers import controller

    live = threading.Event()
    controller.track_compose_child('sib', _StillWorking(live))
    assert controller.compose_inflight('sib') is True

    real_thread = threading.Thread
    monkeypatch.setattr(
        threading, 'Thread',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('cannot start thread')))
    try:
        controller.track_compose_child('sib', _StillWorking(None))
    finally:
        monkeypatch.setattr(threading, 'Thread', real_thread)

    assert controller.compose_inflight('sib') is True, (
        "the failed spawn erased a running sibling's mark")
    live.set()


@pytest.mark.asyncio
async def test_a_deferred_pass_comes_back_in_minutes_not_hours(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reboot case the startup jitter alone does not cover.

    An instance sidecar and the greffer come up together, so the early pass
    can land inside the sidecar's 90s settle window and be thrown away. The
    certificate it would have renewed survived the reboot on its volume,
    already expired, so waiting a full interval for the next attempt puts
    the node right back where the startup jitter was meant to rescue it.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(
        cert_renewal, 'renew_all',
        lambda *a, **kw: _result(renewed=0, skipped=1, deferred=1))
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept[1] == cert_renewal._DEFERRED_RETRY_SECONDS, slept


@pytest.mark.asyncio
async def test_a_pass_that_deferred_nothing_keeps_the_full_interval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backoff or a missing published port is not a reason to come back in
    two minutes -- one is a deliberate wait, the other needs an operator."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(
        cert_renewal, 'renew_all',
        lambda *a, **kw: _result(renewed=0, skipped=1, deferred=0))
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept[1] == settings.greffer_cert_renewal_interval, slept


@pytest.mark.asyncio
async def test_a_tick_displaced_by_an_operator_retries_soon(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on c79acda: the collision costs the whole fleet its turn.

    An operator's targeted `renew_certs --instance X` renews X and nothing
    else, but it holds the pass lock, so the scheduled tick it displaces is
    discarded entirely. Every other due instance on the node then waits a
    full interval because of a collision that lasted a minute.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'
    app.state.registered = asyncio.Event()
    app.state.registered.set()
    slept: list = []

    async def _sleep(n):
        slept.append(n)
        if len(slept) >= 2:
            raise asyncio.CancelledError

    def _busy(*a, **kw):
        raise cert_renewal.RenewalAlreadyRunning

    monkeypatch.setattr(asyncio, 'sleep', _sleep)
    monkeypatch.setattr(cert_renewal, 'renew_all', _busy)
    with pytest.raises(asyncio.CancelledError):
        await cert_renewal.cert_renewal_worker(app)

    assert slept[1] == cert_renewal._DEFERRED_RETRY_SECONDS, slept
