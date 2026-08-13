"""Tests for the instance upstream-certificate renewal worker.

The defect this worker closes is not "no certificate was fetched" -- it is
"a certificate was fetched, written, and never served". So the tests that
matter here are the ones where every step reports success and the sidecar is
still serving the old serial. A suite that only checks the happy path would
pass against the bug.
"""

from __future__ import annotations

import datetime as dt
import time
from unittest.mock import patch

import pytest

from app.settings import Settings
from app.workers import cert_renewal

NEW = 'aa11bb22'
OLD = '99887766'
# Serial baked into the real certificate the tls_listener fixture generates.
PROBE_SERIAL = 'aa11bb22'


def _cert(serial: str = NEW) -> dict:
    return {
        'certificate': f'-----BEGIN CERTIFICATE-----\n{serial}\n-----END CERTIFICATE-----',
        'private_key': '-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----',
        'issuing_ca': '-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----',
        'serial_number': serial,
    }


def _expiring(days: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)


@pytest.fixture(autouse=True)
def _no_carried_backoff():
    """The backoff map is module state; a leak makes later tests silently skip."""
    cert_renewal._renewal_backoff.clear()
    yield
    cert_renewal._renewal_backoff.clear()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Stub the docker/network edges; keep the orchestration real."""
    calls: dict[str, list] = {'install': [], 'reload': [], 'restart': [], 'report': []}
    # No settle wait in the orchestration tests: _await_serial then probes
    # exactly once, so the sequence stubs below line up step-for-step. The
    # settling behaviour itself has its own tests, which do not stub it.
    monkeypatch.setattr(cert_renewal, '_SETTLE_SECONDS', 0)
    monkeypatch.setattr(cert_renewal, '_SETTLE_POLL_SECONDS', 0)
    monkeypatch.setattr(cert_renewal, '_sidecar_host_port', lambda s, g: 45347)
    monkeypatch.setattr(cert_renewal, '_install', lambda g, c: calls['install'].append(c['serial_number']))
    monkeypatch.setattr(cert_renewal, '_reload', lambda g: calls['reload'].append(g) or True)
    monkeypatch.setattr(cert_renewal, '_restart', lambda g: calls['restart'].append(g) or True)
    monkeypatch.setattr(cert_renewal, '_report', lambda s, t, g, serial: calls['report'].append(serial))
    return calls


def test_a_reload_that_did_not_take_escalates_to_a_restart(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. Every call succeeds; the sidecar serves the old serial.

    `nginx -s reload` exits 0 for a signal it delivered, not for a config the
    master adopted. On the stock sidecar image nothing else would ever notice,
    which is how the equivalent bug on the greffer's own nginx survived four
    years.
    """
    served = [(OLD, _expiring(1)), (OLD, None), (NEW, None)]  # before, after reload, after restart
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['restart'] == ['inst-1'], 'a reload that did not take must restart the sidecar'
    assert result == NEW
    assert wired['report'] == [NEW]


def test_a_reload_that_took_does_not_restart(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """The restart is the fallback, not the mechanism.

    Restarting every renewal would drop connections on every instance every
    30 days for no reason.
    """
    served = [(OLD, _expiring(1)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['restart'] == []
    assert wired['report'] == [NEW]


def test_a_renewal_that_never_takes_reports_the_serial_actually_served(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure is reported, not swallowed. Reporting is how it gets cleaned up.

    `instance_cert_installed` is a report, not a confirmation: it compares the
    value against its own pending record, and the mismatch branch is what
    retires the dead mint into the prunable orphan ledger and starts the
    cooldown. Silence leaves a live 30-day SERVER_AUTH cert the manager cannot
    revoke, count or prune -- and re-mints another next tick.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['restart'] == ['inst-1'], 'it should still have tried the restart'
    assert wired['report'] == [OLD], 'the manager must be told what is really served'
    assert result == OLD


def test_an_unreadable_sidecar_still_reports(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """No handshake at all is a truthful report, not a reason to stay quiet.

    The manager reads an unparseable serial as a mismatch, which is exactly
    right: we minted a certificate and cannot show it is serving.
    """
    served = [(OLD, _expiring(1)), (None, None), (None, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['report'] == [None]


def test_a_stuck_instance_stops_minting_every_tick(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backoff exists to stop ISSUANCE, not to save work.

    Every attempt that reaches the mint burns a live 30-day cert from a 30/hour
    per-greffer budget. A stuck instance retried on the fixed tick crowds the
    node's healthy instances out of that budget and 429s them toward the expiry
    this worker exists to prevent.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    minted: list[str] = []
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: minted.append(g) or _cert())

    for _ in range(3):
        cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert minted == ['inst-1'], 'the 2nd and 3rd tick must not re-mint'


def test_backoff_grows_and_is_capped(settings: Settings) -> None:
    """Capped, or the backoff becomes the expiry.

    A day's cap leaves ~7 attempts inside the default 7-day window, so a
    transiently broken instance still recovers on its own.
    """
    for _ in range(40):
        cert_renewal._note_failure('inst-1', settings.cert_renewal_interval)
    failures, deadline = cert_renewal._renewal_backoff['inst-1']
    assert failures == 40
    assert deadline - time.monotonic() <= cert_renewal._BACKOFF_CAP_SECONDS


def test_a_recovered_instance_is_renewed_again(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success clears the backoff, or one bad week would mute an instance for good."""
    served = [(OLD, _expiring(1)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    cert_renewal._note_failure('inst-1', settings.cert_renewal_interval)
    cert_renewal._renewal_backoff['inst-1'] = (1, time.monotonic() - 1)  # elapsed

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert 'inst-1' not in cert_renewal._renewal_backoff


def test_a_healthy_certificate_is_left_alone(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the window, nothing is minted at all.

    Without this the worker would mint on every tick, burning the endpoint's
    rate limit and rotating certificates every six hours.
    """
    monkeypatch.setattr(
        cert_renewal,
        '_served_certificate',
        lambda h, p: (OLD, _expiring(settings.cert_renewal_window_days + 5)),
    )
    minted = []
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: minted.append(g) or _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert minted == []
    assert wired['install'] == []


def test_an_unreadable_expiry_is_treated_as_due(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown is due, not healthy.

    The failure being fixed is a certificate nobody renewed, so "we cannot
    tell" must fall on the side of asking. The mint endpoint is rate-limited
    and 404s when renewal is off, so the cost of asking is bounded.
    """
    served = [(None, None), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['install'] == [NEW]


def test_a_manager_with_renewal_off_is_not_an_error(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """404 means "do not renew", and must leave the instance untouched."""
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: None)

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['install'] == []
    assert wired['restart'] == []
    assert result == OLD


def test_one_bad_instance_does_not_end_the_tick(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Instances expire on their own schedules.

    A single unreachable sidecar aborting the loop would take the whole fleet
    down with it, and the failure would compound silently until every
    certificate on the machine had expired.
    """
    monkeypatch.setattr(cert_renewal, 'collect_status_map', lambda s: {}, raising=False)
    monkeypatch.setattr(cert_renewal, 'resolve_token', lambda s: 'tok')
    seen: list[str] = []

    def _one(s, t, greffon_id, force=False):
        seen.append(greffon_id)
        if greffon_id == 'bad':
            raise RuntimeError('sidecar unreachable')
        return NEW

    monkeypatch.setattr(cert_renewal, 'renew_one', _one)
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'bad': 'running', 'good': 'running'},
    ):
        cert_renewal.renew_all(settings)

    assert seen == ['bad', 'good']


def test_a_stopped_instance_is_skipped(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """No sidecar to reload, and start re-mints anyway."""
    monkeypatch.setattr(cert_renewal, 'resolve_token', lambda s: 'tok')
    seen: list[str] = []
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: seen.append(g))
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'up': 'running', 'down': 'stopped'},
    ):
        cert_renewal.renew_all(settings)

    assert seen == ['up']


@pytest.mark.parametrize(
    ('a', 'b', 'same'),
    [
        ('AA11BB22', 'aa11bb22', True),  # openssl uppercases
        ('0aa11bb22', 'aa11bb22', True),  # leading zero from one side only
        ('aa11bb22', 'aa11bb23', False),
        (None, 'aa11bb22', False),
        ('', 'aa11bb22', False),
    ],
)
def test_serial_comparison_survives_formatting(a, b, same) -> None:
    """A formatting difference read as a mismatch restarts a healthy sidecar."""
    assert cert_renewal._same_serial(a, b) is same


def test_force_renews_through_the_backoff(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """`renew_certs --force` is for an instance that is ALREADY 502ing.

    Backoff protects the mint budget, but an operator staring at a dead app
    cannot be told to wait a day for it to elapse. Forcing skips the local
    guard only; the manager re-checks the window, cooldown and cap itself.
    """
    served = [(OLD, _expiring(1)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    cert_renewal._note_failure('inst-1', settings.cert_renewal_interval)

    cert_renewal.renew_one(settings, 'tok', 'inst-1', force=True)

    assert wired['install'] == [NEW]


def test_force_renews_a_certificate_that_is_not_due(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """The greffer reads the expiry off the served cert, so a sidecar serving a
    stale-but-valid one looks healthy to it. The manager holds the real record,
    so forcing hands it the decision rather than making one here."""
    served = [(OLD, _expiring(settings.cert_renewal_window_days + 20)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1', force=True)

    assert wired['install'] == [NEW]


def test_only_leaves_the_rest_of_the_fleet_alone(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--instance` must not renew the whole node as a side effect."""
    monkeypatch.setattr(cert_renewal, 'resolve_token', lambda s: 'tok')
    seen: list[str] = []
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: seen.append(g))
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'a': 'running', 'b': 'running'},
    ):
        cert_renewal.renew_all(settings, only='b')

    assert seen == ['b']


def test_the_tick_reports_how_many_instances_failed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's exit code is this count, so an operator script can branch on it."""
    monkeypatch.setattr(cert_renewal, 'resolve_token', lambda s: 'tok')

    def _boom(s, t, g, force=False):
        raise RuntimeError('sidecar unreachable')

    monkeypatch.setattr(cert_renewal, 'renew_one', _boom)
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'a': 'running', 'b': 'running', 'c': 'stopped'},
    ):
        assert cert_renewal.renew_all(settings) == 2


def test_a_slow_reload_is_not_a_mismatch(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """nginx keeps answering with the OLD cert for a moment after SIGHUP.

    The master re-reads config, forks new workers and drains the old ones, and
    the drainers still hold the shared listening socket. Believing the first
    probe would restart a healthy sidecar on every renewal -- turning the
    zero-downtime rotation this design is built around into a connection-
    dropping bounce for every instance.
    """
    monkeypatch.setattr(cert_renewal, '_SETTLE_SECONDS', 5)
    monkeypatch.setattr(cert_renewal, '_SETTLE_POLL_SECONDS', 0.01)
    served = [(OLD, _expiring(1)), (OLD, None), (OLD, None), (NEW, None)]
    monkeypatch.setattr(
        cert_renewal, '_served_certificate',
        lambda h, p: served.pop(0) if served else (NEW, None))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['restart'] == [], 'a sidecar that just needed a moment must not be bounced'
    assert wired['report'] == [NEW]


def test_the_settle_wait_is_bounded(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sidecar that never comes back must not park the tick forever."""
    monkeypatch.setattr(cert_renewal, '_SETTLE_SECONDS', 0.2)
    monkeypatch.setattr(cert_renewal, '_SETTLE_POLL_SECONDS', 0.01)
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    started = time.monotonic()
    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert time.monotonic() - started < 5, 'the settle wait must be bounded'
    assert wired['restart'] == ['inst-1']


def test_a_renewal_never_races_a_start_on_the_same_sidecar(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewal holds the same per-instance lock as start / stop / backup.

    Both writers put a key and a certificate into the same /etc/nginx while one
    of them is recreating the container. Interleaved, they can leave nginx with
    one side's key and the other's certificate, which does not load at all --
    and the manager cannot tell afterwards which cert is live, so it refuses to
    revoke either.
    """
    from app.backup import _instance_lock

    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    held = _instance_lock('inst-1')
    assert held.acquire(blocking=False)
    try:
        result = cert_renewal.renew_one(settings, 'tok', 'inst-1')
    finally:
        held.release()

    assert result is None
    assert wired['install'] == [], 'must not write into a sidecar another op holds'
    assert 'inst-1' not in cert_renewal._renewal_backoff, 'busy is not a failure'


def test_a_permanent_refusal_backs_off(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """409 cert_cn_would_not_match can never self-resolve on its own."""
    class _Res:
        status_code = 409
        text = '{"message": "cert_cn_would_not_match"}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda *a, **k: _Res())

    assert cert_renewal._mint(settings, 'tok', 'inst-1') is None
    assert 'inst-1' in cert_renewal._renewal_backoff


def test_a_departed_instance_is_dropped_from_the_backoff_map(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise `backing_off` -- the one health number this worker emits --
    counts instances the node no longer runs, forever."""
    monkeypatch.setattr(cert_renewal, 'resolve_token', lambda s: 'tok')
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: None)
    cert_renewal._note_failure('gone', settings.cert_renewal_interval)
    cert_renewal._note_failure('here', settings.cert_renewal_interval)

    with patch('app.workers.status_collect.collect_status_map',
               return_value={'here': 'running'}):
        cert_renewal.renew_all(settings)

    assert set(cert_renewal._renewal_backoff) == {'here'}


# ---------------------------------------------------------------------------
# The real edges. Every test above stubs _served_certificate / _reload, which
# is exactly how three defects shipped green: the orchestration was verified
# and the things it orchestrates were never called once.
# ---------------------------------------------------------------------------

@pytest.fixture
def tls_listener(tmp_path):
    """A real TLS server presenting a real certificate with a known serial."""
    import socket
    import ssl
    import subprocess
    import threading

    crt, key = tmp_path / 'c.pem', tmp_path / 'k.pem'
    subprocess.run(
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', str(key),
         '-out', str(crt), '-days', '3', '-nodes', '-subj', '/CN=localhost',
         # Derived from the hex, never hand-converted: an earlier revision
         # asserted against a decimal that was not this serial at all.
         '-set_serial', str(int(PROBE_SERIAL, 16))],
        check=True, capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(8)
    stop = threading.Event()

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except OSError:
                return
            except ssl.SSLError:
                continue

    threading.Thread(target=_serve, daemon=True).start()
    yield srv.getsockname()[1]
    stop.set()
    srv.close()


def test_the_probe_reads_a_real_certificate(tls_listener) -> None:
    """Reads BOTH values off a live handshake.

    The expiry is the one that broke: under CERT_NONE, CPython returns an empty
    dict from getpeercert(), always and on every build. Reading notAfter from
    there gave None for every instance, which made every instance permanently
    "due" and the not-due short-circuit dead code -- and no stubbed test could
    see it, because the stub returned a datetime the real function never could.
    """
    serial, not_after = cert_renewal._served_certificate('127.0.0.1', tls_listener)

    assert cert_renewal._same_serial(serial, PROBE_SERIAL), serial
    assert not_after is not None, 'the expiry must survive CERT_NONE'
    remaining = not_after - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(days=2) < remaining < dt.timedelta(days=4)


def test_the_probe_reports_cannot_tell_for_a_dead_sidecar() -> None:
    """A closed port must read as unknown, not raise out of the tick."""
    import socket

    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()  # nothing is listening now

    assert cert_renewal._served_certificate('127.0.0.1', port) == (None, None)


def test_the_probe_does_not_hang_on_a_peer_that_never_speaks_tls() -> None:
    """A plain TCP accept with no TLS behind it must time out, not park the
    worker thread for the life of the process."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    accepted: list = []
    threading.Thread(target=lambda: accepted.append(srv.accept()), daemon=True).start()
    try:
        cert_renewal._PROBE_TIMEOUT_SECONDS_ORIG = cert_renewal._PROBE_TIMEOUT_SECONDS
        cert_renewal._PROBE_TIMEOUT_SECONDS = 1
        started = time.monotonic()
        assert cert_renewal._served_certificate(
            '127.0.0.1', srv.getsockname()[1]) == (None, None)
        assert time.monotonic() - started < 5
    finally:
        cert_renewal._PROBE_TIMEOUT_SECONDS = cert_renewal._PROBE_TIMEOUT_SECONDS_ORIG
        srv.close()


def test_reload_hands_docker_a_container_not_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """exec_in_container dereferences container.id.

    Passing the name string raised AttributeError, which its own error tuple
    does not catch, so every renewal aborted AFTER writing the new files: no
    reload, no verification, no report, no backoff. The manager kept a pending
    record the greffer never resolved and certificates never rotated. Every
    test in this file stubbed _reload, so all of them passed.
    """
    import apps.utils.docker.base as docker_base
    from apps.utils.docker import exec_op

    class _Container:
        id = 'deadbeef'

    seen: list = []
    monkeypatch.setattr(
        docker_base, 'client',
        type('C', (), {'containers': type('Cs', (), {
            'get': staticmethod(lambda name: _Container())})()})())
    monkeypatch.setattr(
        exec_op, 'exec_in_container',
        lambda container, argv: seen.append((container, argv))
        or type('R', (), {'ok': True})())

    assert cert_renewal._reload('inst-1') is True
    (container, argv), = seen
    assert container.id == 'deadbeef', 'must pass the container object'
    assert argv == ['nginx', '-s', 'reload']


def test_a_docker_failure_in_reload_falls_through_to_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload is best-effort; the restart escalation is what must still run."""
    import apps.utils.docker.base as docker_base

    def _boom(name):
        raise RuntimeError('docker is hung')

    monkeypatch.setattr(
        docker_base, 'client',
        type('C', (), {'containers': type('Cs', (), {
            'get': staticmethod(_boom)})()})())

    assert cert_renewal._reload('inst-1') is False


def test_an_instance_that_raises_still_backs_off(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loudest failures must not be the ones that retry hardest.

    A deleted sidecar or an unreachable manager raises out of _mint / _install
    rather than returning a mismatch, and those repeat. Without arming the
    backoff here they retry at the full tick rate forever, each one burning a
    fresh 30-day mint from the budget the backoff exists to protect.
    """
    monkeypatch.setattr(cert_renewal, 'resolve_token', lambda s: 'tok')

    def _boom(s, t, g, force=False):
        raise RuntimeError('sidecar is gone')

    monkeypatch.setattr(cert_renewal, 'renew_one', _boom)
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'inst-1': 'running'}):
        cert_renewal.renew_all(settings)

    assert 'inst-1' in cert_renewal._renewal_backoff
