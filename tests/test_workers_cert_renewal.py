"""Tests for the instance upstream-certificate renewal worker.

The defect this worker closes is not "no certificate was fetched" -- it is
"a certificate was fetched, written, and never served". So the tests that
matter here are the ones where every step reports success and the sidecar is
still serving the old serial. A suite that only checks the happy path would
pass against the bug.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from types import SimpleNamespace
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
    cert_renewal._unconfirmed.clear()
    cert_renewal._stop.clear()
    yield
    cert_renewal._renewal_backoff.clear()
    cert_renewal._unconfirmed.clear()
    cert_renewal._stop.clear()


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
    assert result == cert_renewal.Outcome(cert_renewal.RENEWED, NEW)
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
    assert result == cert_renewal.Outcome(cert_renewal.FAILED, OLD)


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
        cert_renewal._note_failure('inst-1', settings.greffer_cert_renewal_interval)
    failures, deadline = cert_renewal._renewal_backoff['inst-1']
    assert failures == 40
    # A LITERAL, not the constant: asserting against _BACKOFF_CAP_SECONDS is
    # circular, so widening the cap to 30 days passed this test unchanged.
    assert deadline - time.monotonic() <= 24 * 60 * 60


def test_a_recovered_instance_is_renewed_again(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success clears the backoff, or one bad week would mute an instance for good."""
    served = [(OLD, _expiring(1)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    cert_renewal._note_failure('inst-1', settings.greffer_cert_renewal_interval)
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
        lambda h, p: (OLD, _expiring(settings.greffer_cert_renewal_window_days + 5)),
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


def test_an_instance_the_manager_no_longer_knows_is_a_failure(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """_mint returning None now means ONE thing: 'not_found', the instance is
    gone from the manager while this node still runs it. That is a real
    anomaly and stays FAILED. The fleet-wide 'renewal is off' 404 raises
    RenewalUnavailable instead -- see the two tests below."""
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: None)

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['install'] == []
    assert wired['restart'] == []
    assert result == cert_renewal.Outcome(cert_renewal.FAILED, OLD)


def test_one_bad_instance_does_not_end_the_tick(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Instances expire on their own schedules.

    A single unreachable sidecar aborting the loop would take the whole fleet
    down with it, and the failure would compound silently until every
    certificate on the machine had expired.
    """
    monkeypatch.setattr(cert_renewal, 'collect_status_map', lambda s: {}, raising=False)
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
        cert_renewal.renew_all(settings, 'tok')

    assert seen == ['bad', 'good']


def test_a_stopped_instance_is_skipped(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """No sidecar to reload, and start re-mints anyway."""
    seen: list[str] = []
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: seen.append(g))
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'up': 'running', 'down': 'stopped'},
    ):
        cert_renewal.renew_all(settings, 'tok')

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
    cert_renewal._note_failure('inst-1', settings.greffer_cert_renewal_interval)

    cert_renewal.renew_one(settings, 'tok', 'inst-1', force=True)

    assert wired['install'] == [NEW]


def test_force_renews_a_certificate_that_is_not_due(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """The greffer reads the expiry off the served cert, so a sidecar serving a
    stale-but-valid one looks healthy to it. The manager holds the real record,
    so forcing hands it the decision rather than making one here."""
    served = [(OLD, _expiring(settings.greffer_cert_renewal_window_days + 20)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1', force=True)

    assert wired['install'] == [NEW]


def test_only_leaves_the_rest_of_the_fleet_alone(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--instance` must not renew the whole node as a side effect."""
    seen: list[str] = []
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: seen.append(g))
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'a': 'running', 'b': 'running'},
    ):
        cert_renewal.renew_all(settings, 'tok', only='b')

    assert seen == ['b']


def test_the_tick_reports_how_many_instances_failed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's exit code is this count, so an operator script can branch on it."""

    def _boom(s, t, g, force=False):
        raise RuntimeError('sidecar unreachable')

    monkeypatch.setattr(cert_renewal, 'renew_one', _boom)
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'a': 'running', 'b': 'running', 'c': 'stopped'},
    ):
        assert cert_renewal.renew_all(settings, 'tok').errors == 2


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

    assert result == cert_renewal.Outcome(
        cert_renewal.SKIPPED, None, 'instance_busy'), (
        'busy must honour the Outcome contract: returning None made the caller '
        'raise AttributeError, which the broad handler then counted as an '
        'error AND armed the backoff -- so a nightly backup overlapping the '
        'tick could push an instance to the 24h cap and expire its cert'
    )
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
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: None)
    cert_renewal._note_failure('gone', settings.greffer_cert_renewal_interval)
    cert_renewal._note_failure('here', settings.greffer_cert_renewal_interval)

    with patch('app.workers.status_collect.collect_status_map',
               return_value={'here': 'running'}):
        cert_renewal.renew_all(settings, 'tok')

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

    def _boom(s, t, g, force=False):
        raise RuntimeError('sidecar is gone')

    monkeypatch.setattr(cert_renewal, 'renew_one', _boom)
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'inst-1': 'running'}):
        cert_renewal.renew_all(settings, 'tok')

    assert 'inst-1' in cert_renewal._renewal_backoff


# ---------------------------------------------------------------------------
# The functions the `wired` fixture replaces. Without these, five simultaneous
# fatal mutations (swapped key/cert contents, compose-v1 container name, wrong
# report route and body key, container-port instead of host-port, dead
# off-switch) all left the suite green.
# ---------------------------------------------------------------------------

def test_install_writes_the_pair_in_one_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both files, right names, right contents, one extraction.

    One archive because two put_archive calls leave a window holding a NEW key
    against the OLD certificate. nginx serves from memory so nothing looks
    wrong, until the sidecar restarts weeks later for an unrelated reason and
    crash-loops on the mismatched pair.
    """
    import tarfile

    import apps.utils.docker.base as docker_base

    captured: list = []

    class _Container:
        @staticmethod
        def put_archive(path, stream):
            captured.append((path, stream.read()))
            return True

    monkeypatch.setattr(
        docker_base, 'client',
        type('C', (), {'containers': type('Cs', (), {
            'get': staticmethod(lambda name: _Container())})()})())

    cert_renewal._install('inst-1', _cert())

    assert len(captured) == 1, 'the pair must land in a single extraction'
    path, blob = captured[0]
    assert path == '/etc/nginx'
    with tarfile.open(fileobj=__import__('io').BytesIO(blob)) as tar:
        members = {m.name: m for m in tar.getmembers()}
        got = {n: tar.extractfile(m).read().decode() for n, m in members.items()}
    assert set(got) == {'cert.key', 'pem.crt'}
    assert 'PRIVATE KEY' in got['cert.key'], 'key and certificate are swapped'
    assert 'CERTIFICATE' in got['pem.crt'], 'key and certificate are swapped'
    # tar defaults to 0644, while the START path stages this key through
    # mkstemp (0600) and docker cp preserves it. Unasserted, every renewal
    # silently widened an unencrypted TLS private key to world-readable.
    assert members['cert.key'].mode == 0o600, oct(members['cert.key'].mode)
    assert members['pem.crt'].mode == 0o644, oct(members['pem.crt'].mode)


def test_the_sidecar_container_name_matches_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """<project>-<service>-1, where the project is the instance id.

    compose.py runs `docker-compose -p <instance id>` and names the service
    `greffon_nginx`; compose v2 joins them with hyphens. A v1-style name
    (underscores) resolves to no container at all, so every docker call in this
    worker would fail against a healthy sidecar.
    """
    import inspect

    from apps.utils.docker import compose

    # Tie the two halves to their real sources rather than restating the string:
    # the service key compose writes, and the project name it passes to -p.
    src = inspect.getsource(compose)
    assert "compose['services']['greffon_nginx']" in src
    assert "'-p', greffon_info['id']" in src

    assert cert_renewal._nginx_container('abc-123') == 'abc-123-greffon_nginx-1'


def test_the_probe_uses_the_host_published_port(settings: Settings, tmp_path) -> None:
    """The HOST side of the mapping, not the container side.

    "45347:8000" -- 8000 is inside the sidecar's network namespace and means
    nothing from the greffer's. Taking the wrong half makes every verification
    dial a closed port.
    """
    inst = tmp_path / 'inst-1'
    inst.mkdir()
    (inst / 'docker-compose.yml').write_text(
        'services:\n'
        '  greffon_nginx:\n'
        '    image: nginx\n'
        '    ports:\n'
        '      - "45347:8000"\n'
    )
    settings.greffon_path = tmp_path

    assert cert_renewal._sidecar_host_port(settings, 'inst-1') == 45347


def test_a_missing_compose_is_not_an_exception(settings: Settings, tmp_path) -> None:
    """An instance dir removed mid-tick must skip, not end the tick."""
    settings.greffon_path = tmp_path
    assert cert_renewal._sidecar_host_port(settings, 'not-there') is None


def test_report_posts_the_served_serial_to_the_right_route(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route, body key and auth header are a contract with the manager.

    `instance_cert_installed` reads exactly `served_serial` and authenticates on
    X-Greffer-Token; anything else is a silent no-op that leaves the mint
    pending forever.
    """
    seen: dict = {}

    class _Res:
        status_code = 200
        text = '{"message": "confirmed"}'

    def _post(url, **kw):
        seen['url'] = url
        seen.update(kw)
        return _Res()

    monkeypatch.setattr(cert_renewal.requests, 'post', _post)
    cert_renewal._report(settings, 'tok-123', 'inst-1', NEW)

    assert seen['url'].endswith('/api/greffer/instances/inst-1/cert-installed/')
    assert seen['json'] == {'served_serial': NEW}
    assert seen['headers'] == {'X-Greffer-Token': 'tok-123'}
    assert seen['verify'] == settings.greffer_ssl_verify


def test_a_dropped_confirmation_is_retried(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost 200 is otherwise terminal.

    The certificate is installed and serving, so the next tick reads it as
    not-due and never reports again: the manager's cert_serial stays pinned to
    a superseded value and alarms on a healthy instance. The manager's own 503
    exists so the greffer re-reports.
    """
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)
    attempts: list = []

    class _Res:
        def __init__(self, code):
            self.status_code = code
            self.text = ''

    def _post(url, **kw):
        attempts.append(url)
        return _Res(200 if len(attempts) == 3 else 503)

    monkeypatch.setattr(cert_renewal.requests, 'post', _post)
    cert_renewal._report(settings, 'tok', 'inst-1', NEW)

    assert len(attempts) == 3, 'a 503 must be retried, not accepted'


def test_a_rejected_report_is_not_retried(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """409 means the manager acted. Retrying cannot change the answer and the
    pending it referred to is already gone."""
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)
    attempts: list = []

    class _Res:
        status_code = 409
        text = '{"message": "serial_mismatch"}'

    monkeypatch.setattr(cert_renewal.requests, 'post',
                        lambda url, **kw: attempts.append(url) or _Res())
    cert_renewal._report(settings, 'tok', 'inst-1', NEW)

    assert len(attempts) == 1


def test_mint_returns_the_manager_payload(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Res:
        status_code = 200
        text = ''

        @staticmethod
        def json():
            return _cert()

    seen: dict = {}

    def _post(url, **kw):
        seen['url'] = url
        seen.update(kw)
        return _Res()

    monkeypatch.setattr(cert_renewal.requests, 'post', _post)
    got = cert_renewal._mint(settings, 'tok-123', 'inst-1')

    assert got['serial_number'] == NEW
    assert seen['url'].endswith('/api/greffer/instances/inst-1/cert/')
    assert seen['headers'] == {'X-Greffer-Token': 'tok-123'}
    # The mint RESPONSE carries the instance's unencrypted private key, and
    # `never verify=False on requests calls` is a project rule. Unasserted, the
    # mutation to verify=False passed the whole suite.
    assert seen['verify'] == settings.greffer_ssl_verify
    assert seen['timeout'] == cert_renewal._HTTP_TIMEOUT_SECONDS


def test_a_node_wide_rate_limit_is_not_charged_to_the_instance(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-greffer 30/h cap says nothing about THIS instance.

    Backing it off exponentially pushes healthy instances behind a limit none
    of them caused, and the node then uses a fraction of its own budget.
    """
    class _Res:
        status_code = 429
        text = '{"message": "rate_limited"}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda *a, **k: _Res())

    with pytest.raises(cert_renewal._NodeRateLimited):
        cert_renewal._mint(settings, 'tok', 'inst-1')
    assert 'inst-1' not in cert_renewal._renewal_backoff


def test_a_capped_node_stops_the_tick(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every remaining instance would collect the same refusal."""
    seen: list = []

    def _one(s, t, g, force=False):
        seen.append(g)
        raise cert_renewal._NodeRateLimited

    monkeypatch.setattr(cert_renewal, 'renew_one', _one)
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'a': 'running', 'b': 'running', 'c': 'running'}), \
            pytest.raises(cert_renewal.NodeCapped):
        cert_renewal.renew_all(settings, 'tok')

    assert seen == ['a'], 'the tick must stop at the cap, not grind through it'


def test_a_blind_probe_path_stops_the_tick(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sidecars answering nothing in a row is a statement about the greffer.

    A firewall rule, a missing host-gateway alias or a hung daemon would
    otherwise have the tick mint, fail to verify, restart and back off every
    instance on the node in one pass.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (None, None))
    minted: list = []
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: minted.append(g) or _cert())
    # Well above _BLIND_STREAK_ABORT: sized AT the constant, raising the
    # constant to the fleet size made the abort a no-op and still passed.
    fleet = {f'i{n}': 'running' for n in range(40)}

    with patch('app.workers.status_collect.collect_status_map', return_value=fleet):
        cert_renewal.renew_all(settings, 'tok')

    assert len(minted) == 3, (
        f'minted {len(minted)} certificates against an unobservable fleet')


def test_an_unobservable_sidecar_is_not_restarted(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never bounce a container you cannot see.

    Restarting on "no answer" turns one broken probe path into a rolling
    restart of every healthy instance on the node, every tick.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (None, None))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['restart'] == [], 'must not restart an unobservable sidecar'
    assert wired['report'] == [None], 'but must still report the truth'


@pytest.mark.asyncio
async def test_the_worker_off_switch_actually_returns(settings: Settings) -> None:
    """A dead off-switch is worse than none: it reads as disabled and renews."""
    from fastapi import FastAPI

    settings.greffer_cert_renewal_enabled = False
    app = FastAPI()
    app.state.settings = settings
    app.state.greffer_token = 'tok'

    # Returns rather than sleeping for the interval.
    await asyncio.wait_for(cert_renewal.cert_renewal_worker(app), timeout=5)


def test_two_renewal_passes_do_not_interleave(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The periodic tick and the operator's call land in the SAME process.

    Interleaved, they trample the shared blind-streak counter and backoff map,
    race each other for the same per-instance locks, and split one per-greffer
    mint budget two ways. There is no reason to run two.
    """
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g, force=False: None)

    assert cert_renewal._pass_lock.acquire(blocking=False)
    try:
        with pytest.raises(cert_renewal.RenewalAlreadyRunning):
            cert_renewal.renew_all(settings, 'tok')
    finally:
        cert_renewal._pass_lock.release()


def test_the_pass_lock_is_released_after_a_failed_tick(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that raises must not wedge renewal on this node for good."""

    def _boom(s):
        raise RuntimeError('docker is gone')

    monkeypatch.setattr(cert_renewal, '_renew_all_locked',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError):
        cert_renewal.renew_all(settings, 'tok')

    assert cert_renewal._pass_lock.acquire(blocking=False), 'lock leaked'
    cert_renewal._pass_lock.release()


# ---------------------------------------------------------------------------
# Codex round. Each of these fails against the code as it stood before it.
# ---------------------------------------------------------------------------

def test_a_failed_reload_still_escalates_to_a_restart(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reload that errored is the strongest reason to restart, not a reason to give up.

    Skipping the probe when `nginx -s reload` failed denied a reachable,
    restartable sidecar the one fallback that would have fixed it -- so it kept
    serving the old certificate until expiry, which is the bug.
    """
    served = [(OLD, _expiring(1)), (OLD, None), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_reload', lambda g: False)
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['restart'] == ['inst-1']
    assert result.status == cert_renewal.RENEWED


def test_an_owed_confirmation_is_retried_on_the_next_pass(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped confirmation is otherwise permanent.

    The certificate IS serving, so every later tick reads it as not-due and
    never reports again: the manager keeps alarming on a healthy instance, its
    pending stays parked as an orphan, and the superseded certificate is never
    revoked. Retrying inside one pass is not enough -- the manager can be down
    for longer than three attempts.
    """
    cert_renewal._unconfirmed['inst-1'] = NEW
    # Serving the new cert already, with plenty of life left: not due.
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (NEW, _expiring(settings.greffer_cert_renewal_window_days + 10)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: pytest.fail('must not re-mint'))

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['report'] == [NEW], 'the owed confirmation must be re-sent'
    assert result.status == cert_renewal.NOT_DUE


def test_an_unconfirmed_report_is_remembered(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)
    monkeypatch.setattr(cert_renewal.requests, 'post',
                        lambda *a, **k: type('R', (), {'status_code': 503, 'text': ''})())

    cert_renewal._report(settings, 'tok', 'inst-1', NEW)

    assert cert_renewal._unconfirmed.get('inst-1') == NEW


def test_a_confirmed_report_clears_the_debt(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    cert_renewal._unconfirmed['inst-1'] = NEW
    monkeypatch.setattr(cert_renewal.requests, 'post',
                        lambda *a, **k: type('R', (), {'status_code': 200, 'text': ''})())

    cert_renewal._report(settings, 'tok', 'inst-1', NEW)

    assert 'inst-1' not in cert_renewal._unconfirmed


def test_the_pass_uses_the_token_it_was_given(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_token MINTS a fresh token when the token file is unreadable.

    That is a supported degraded mode: the app keeps ONE ephemeral token in
    app.state and the manager knows that one. Resolving per pass would present
    a different claimant every time, 403 every renewal, and expire every
    certificate on the node.
    """
    seen: list = []
    monkeypatch.setattr(cert_renewal, 'renew_one',
                        lambda s, t, g, force=False: seen.append(t) or cert_renewal.Outcome(cert_renewal.SKIPPED, None))
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'a': 'running'}):
        cert_renewal.renew_all(settings, 'registered-token')

    assert seen == ['registered-token']


def test_a_refusal_is_counted_as_an_error(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's exit code is this count, and a manager refusal renews nothing.

    Counting only exceptions had `renew_certs` exit 0 after doing nothing,
    telling an operator mid-incident that their emergency recovery worked.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: None)
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'inst-1': 'running'}):
        assert cert_renewal.renew_all(settings, 'tok').errors == 1


def test_an_unmatched_instance_selector_is_an_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd id would otherwise skip everything and report a clean pass."""
    with patch('app.workers.status_collect.collect_status_map',
               return_value={'real-one': 'running'}), \
            pytest.raises(cert_renewal.InstanceNotFound):
        cert_renewal.renew_all(settings, 'tok', only='typo')


def test_a_sidecar_compose_may_still_be_recreating_is_left_alone(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instance lock does not cover the compose child.

    compose.start/stop are subprocess.Popen and the controller releases the
    lock when the HANDLER returns; _wait_for_compose_running only runs on the
    tunnel branch. So in proxy mode the lock is free while compose is still
    recreating the sidecar, and writing a certificate into that window leaves
    the manager unable to tell which cert is live -- it logs
    instance_cert_renewal_orphan at CRITICAL and refuses to revoke either.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    monkeypatch.setattr(cert_renewal, '_sidecar_settling', lambda g: True)

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['install'] == []
    assert result.status == cert_renewal.SKIPPED


def test_a_long_running_sidecar_is_not_treated_as_settling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise nothing would ever renew."""
    import apps.utils.docker.base as docker_base

    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5))
    stamp = old.strftime('%Y-%m-%dT%H:%M:%S.123456789Z')
    monkeypatch.setattr(
        docker_base, 'client',
        type('C', (), {'containers': type('Cs', (), {'get': staticmethod(
            lambda n: type('X', (), {'attrs': {'State': {'StartedAt': stamp}}})())})()})())

    assert cert_renewal._sidecar_settling('inst-1') is False


def test_a_just_started_sidecar_is_treated_as_settling(monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.utils.docker.base as docker_base

    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime('%Y-%m-%dT%H:%M:%S.123456789Z')
    monkeypatch.setattr(
        docker_base, 'client',
        type('C', (), {'containers': type('Cs', (), {'get': staticmethod(
            lambda n: type('X', (), {'attrs': {'State': {'StartedAt': stamp}}})())})()})())

    assert cert_renewal._sidecar_settling('inst-1') is True


def test_the_probe_dials_the_configured_host_not_loopback(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """127.0.0.1 inside the greffer's own netns has no instance ports on it.

    That was round-1 defect (c), and it stayed re-introducible: every
    orchestration test stubs _served_certificate as `lambda h, p` and no test
    ever looked at `h`, so hardcoding loopback passed the entire suite while
    making the feature inert.
    """
    settings.greffer_cert_probe_host = 'host.docker.internal'
    hosts: list = []
    served = [(OLD, _expiring(1)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: hosts.append(h) or served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert hosts and set(hosts) == {'host.docker.internal'}, hosts


def test_the_happy_path_reloads_rather_than_restarting(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reload is the mechanism; the restart is only the fallback.

    `wired` recorded the reload call and nothing ever asserted it, so skipping
    the reload entirely -- degrading every renewal into a connection-dropping
    container restart on every instance every 30 days -- passed the suite.
    """
    served = [(OLD, _expiring(1)), (NEW, None)]
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: served.pop(0))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['reload'] == ['inst-1'], 'the certificate must be reloaded, not restarted into place'
    assert wired['restart'] == []


def test_renewal_stands_off_while_compose_holds_the_instance(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instance lock is the serializer, and start/stop now hold it until
    their compose child exits -- so a renewal cannot overlap a recreate."""
    from app.backup import _instance_lock

    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    monkeypatch.setattr(cert_renewal, '_sidecar_settling', lambda g: False)

    held = _instance_lock('inst-1')
    assert held.acquire(blocking=False)
    try:
        result = cert_renewal.renew_one(settings, 'tok', 'inst-1')
    finally:
        held.release()

    assert wired['install'] == [], 'must not write into a sidecar compose is recreating'
    assert result.status == cert_renewal.SKIPPED


def test_an_issued_cert_is_reported_even_if_installing_it_fails(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mint already happened. Letting the exception escape leaves the
    manager holding a pending nobody resolves."""
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    monkeypatch.setattr(cert_renewal, '_install',
                        lambda g, c: (_ for _ in ()).throw(RuntimeError('no such container')))

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['report'] == [OLD], 'the dead mint was never retired'
    assert result.status == cert_renewal.FAILED
    assert 'inst-1' in cert_renewal._renewal_backoff


def test_a_healthy_certificate_settles_an_old_failure(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something else repaired the instance. Keeping the entry inflates the
    `backing_off` health number forever and makes the next unrelated failure
    resume at the old escalated delay."""
    monkeypatch.setattr(
        cert_renewal, '_served_certificate',
        lambda h, p: (NEW, _expiring(settings.greffer_cert_renewal_window_days + 10)))
    cert_renewal._note_failure('inst-1', settings.greffer_cert_renewal_interval)
    cert_renewal._renewal_backoff['inst-1'] = (4, time.monotonic() - 1)  # elapsed

    cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert 'inst-1' not in cert_renewal._renewal_backoff


def test_renewal_skips_an_instance_compose_is_working_on(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Testing the signal in isolation proves nothing: deleting the CALL to it
    is what lets renewal write into a sidecar mid-recreate."""
    monkeypatch.setattr(cert_renewal, '_served_certificate', lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    monkeypatch.setattr(cert_renewal, '_sidecar_settling', lambda g: False)
    monkeypatch.setattr('app.routers.controller.compose_inflight', lambda g: True)

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert wired['install'] == [], 'wrote into a sidecar compose is recreating'
    assert result.status == cert_renewal.SKIPPED


def test_a_rejected_token_raises_node_wide(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """403 is the manager saying "not you", not "not this instance".

    Recording it as an instance failure backs off every remaining due instance
    on the node for a rotation none of them caused.
    """
    class _Res:
        status_code = 403
        text = '{"message": "invalid_greffer_token"}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda *a, **k: _Res())

    with pytest.raises(cert_renewal.NodeAuthLost):
        cert_renewal._mint(settings, 'stale', 'inst-1')
    assert 'inst-1' not in cert_renewal._renewal_backoff


def test_a_disabled_manager_raises_rather_than_failing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-bodied 404 is CERT_RENEWAL_ENABLED being off fleet-wide.

    This is the entire staged rollout: the image ships, clients run `greffer
    update`, and only then is the manager flag flipped. Treating that window
    as a per-instance failure is a false alarm on every instance, every tick.
    """
    class _Res:
        status_code = 404
        text = '{}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda *a, **k: _Res())

    with pytest.raises(cert_renewal.RenewalUnavailable):
        cert_renewal._mint(settings, 'tok', 'inst-1')
    # And it must not be charged to the instance: a node that renews nothing
    # for a week of rollout would come out the far side backed off on
    # everything, then renew nothing on the day the flag is finally flipped.
    assert 'inst-1' not in cert_renewal._renewal_backoff


def test_a_gone_instance_is_told_apart_from_a_disabled_manager(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same status code, different bodies, opposite meanings."""
    class _Res:
        status_code = 404
        text = '{"message": "not_found"}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda *a, **k: _Res())

    assert cert_renewal._mint(settings, 'tok', 'inst-1') is None


def test_a_disabled_manager_does_not_fail_the_tick(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """errors=0 and a clean return, so `renew_certs` exits 0.

    Codex P2 on bef2c8e: the fleet-wide pass exited non-zero and periodic
    diagnostics reported renewal failures on a deployment whose manager had
    simply not enabled the feature yet.
    """
    asked: list[str] = []

    def _one(s, t, greffon_id, force=False):
        asked.append(greffon_id)
        raise cert_renewal.RenewalUnavailable

    monkeypatch.setattr(cert_renewal, 'renew_one', _one)
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={'a': 'running', 'b': 'running'},
    ):
        result = cert_renewal.renew_all(settings, 'tok')

    assert result.errors == 0
    assert result.skipped == 1
    # Stopped at the first: every remaining instance would collect the same
    # 404, and asking N times is N pointless round trips per tick.
    assert asked == ['a']


def test_a_debt_report_stands_off_while_compose_is_recreating(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar mid-recreate serves nothing, and that is not an answer.

    Codex P2 on 042534d: the pre-lock debt path probed and reported without
    the compose guard the main path uses, so a start or stop overlapping an
    unconfirmed renewal had the transient reading retire the manager's
    pending mint and clear the durable debt that records we still owe one.
    """
    cert_renewal._unconfirmed['inst-1'] = OLD
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (None, None))
    monkeypatch.setattr('app.routers.controller.compose_inflight',
                        lambda g: True)
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        # Nothing reported: not the transient None, not anything.
        assert wired['report'] == []
        # Still owed, so the next quiet tick answers it.
        assert 'inst-1' in cert_renewal._unconfirmed
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_debt_report_stands_off_while_the_instance_lock_is_held(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same standoff for a Start holding the lock rather than Compose."""
    from app.backup import _instance_lock

    cert_renewal._unconfirmed['inst-1'] = OLD
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (None, None))
    monkeypatch.setattr('app.routers.controller.compose_inflight',
                        lambda g: False)
    held = _instance_lock('inst-1')
    assert held.acquire(blocking=False)
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert wired['report'] == []
        assert 'inst-1' in cert_renewal._unconfirmed
    finally:
        held.release()
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_report_to_a_disabled_manager_keeps_the_debt(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on 9238527: the report endpoint is gated on the same flag.

    Turning renewal off between the mint and the report had the empty 404
    read as terminal, clearing the one record that this instance serves
    something the manager has not been told about. A just-minted certificate
    is not due for thirty days, so nothing would reconcile it.
    """
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)

    class _Res:
        status_code = 404
        text = '{}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda url, **kw: _Res())
    try:
        cert_renewal._report(settings, 'tok', 'inst-1', NEW)
        assert cert_renewal._unconfirmed.get('inst-1') == NEW
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_report_for_a_gone_instance_is_terminal(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other 404: nothing to reconcile, so the debt is settled."""
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)

    class _Res:
        status_code = 404
        text = '{"message": "not_found"}'

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda url, **kw: _Res())
    try:
        cert_renewal._report(settings, 'tok', 'inst-1', NEW)
        assert 'inst-1' not in cert_renewal._unconfirmed
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_the_debt_probe_is_serialized_but_the_report_is_not(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The split Codex's P2 on 9238527 asked for, done at the right seam.

    The probe is what has to be serialized against a Compose recreate, and
    it is bounded by _PROBE_TIMEOUT_SECONDS. The report is three attempts on
    a 30s HTTP timeout, and holding the instance lock across it is what
    turns a manager control op into a terminal 409. So: probe under the
    lock, report outside it.

    threading.Lock is not reentrant, so a same-thread acquire that fails is
    proof the lock is held at that moment.
    """
    from app.backup import _instance_lock

    cert_renewal._unconfirmed['inst-1'] = OLD
    lock = _instance_lock('inst-1')
    held: dict[str, list[bool]] = {'probe': [], 'report': []}

    def _watch(phase):
        got = lock.acquire(blocking=False)
        held[phase].append(not got)
        if got:
            lock.release()

    def _probe(host, port):
        _watch('probe')
        return NEW, None

    def _rep(s, t, g, serial):
        _watch('report')

    monkeypatch.setattr(cert_renewal, '_served_certificate', _probe)
    monkeypatch.setattr(cert_renewal, '_report', _rep)
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: None)
    monkeypatch.setattr('app.routers.controller.compose_inflight',
                        lambda g: False)
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert held['probe'][0] is True, 'debt probe must run under the lock'
        assert held['report'][0] is False, 'debt report must not hold the lock'
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_the_settle_skip_says_it_is_transient(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason is what tells the loop whether waiting will help."""
    monkeypatch.setattr(cert_renewal, '_sidecar_settling', lambda g: True)
    monkeypatch.setattr('app.routers.controller.compose_inflight',
                        lambda g: False)

    result = cert_renewal.renew_one(settings, 'tok', 'inst-1')

    assert result == cert_renewal.Outcome(
        cert_renewal.SKIPPED, None, 'sidecar_settling')


def test_only_transient_skips_shorten_the_next_pass(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backoff entry is a deliberate wait measured in hours and a missing
    published port needs an operator. Counting either as deferred would have
    the loop wake every two minutes forever, renewing nothing."""
    # Every transient reason is represented, so dropping any ONE of them
    # from the set fails this rather than passing on the others.
    outcomes = {
        'settling': cert_renewal.Outcome(cert_renewal.SKIPPED, None,
                                         'sidecar_settling'),
        'busy': cert_renewal.Outcome(cert_renewal.SKIPPED, None,
                                     'instance_busy'),
        'recreating': cert_renewal.Outcome(cert_renewal.SKIPPED, None,
                                           'compose_inflight'),
        'backing-off': cert_renewal.Outcome(cert_renewal.SKIPPED, None,
                                            'backoff'),
        'no-port': cert_renewal.Outcome(cert_renewal.SKIPPED, None,
                                        'no_published_port'),
    }
    monkeypatch.setattr(cert_renewal, 'renew_one',
                        lambda s, t, g, force=False: outcomes[g])
    with patch(
        'app.workers.status_collect.collect_status_map',
        return_value={k: 'running' for k in outcomes},
    ):
        result = cert_renewal.renew_all(settings, 'tok')

    assert result.skipped == 5
    assert result.deferred == 3, 'the three that clear on their own, no more'


def test_the_debt_is_on_disk_before_the_first_report_attempt(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on dcf9292: the retry loop is itself a ~96s window.

    Against a manager that accepts the connection and then stalls, three
    attempts on a 30s timeout run for over a minute. A `greffer update`
    inside that window used to leave the sidecar serving a fresh
    certificate with no debt recorded anywhere -- and nothing reconciles it,
    because a just-installed certificate is not due again for thirty days.
    """
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)
    seen: dict = {}

    def _post(url, **kw):
        # What a SIGKILL at this instant would have left behind.
        seen['debt'] = cert_renewal._unconfirmed.get('inst-1')
        raise cert_renewal.requests.RequestException('manager stalled')

    monkeypatch.setattr(cert_renewal.requests, 'post', _post)
    try:
        cert_renewal._report(settings, 'tok', 'inst-1', NEW)
        assert seen['debt'] == NEW, 'nothing owed if the process dies here'
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_terminal_report_still_clears_the_debt(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing it up front must not leave it behind on success."""
    monkeypatch.setattr(cert_renewal, '_REPORT_RETRY_SECONDS', 0)

    class _Res:
        status_code = 200
        text = ''

    monkeypatch.setattr(cert_renewal.requests, 'post', lambda *a, **kw: _Res())
    try:
        cert_renewal._report(settings, 'tok', 'inst-1', NEW)
        assert 'inst-1' not in cert_renewal._unconfirmed
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_the_debt_exists_from_the_moment_a_certificate_is_minted(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on ece0864: the window starts at the mint, not at the report.

    Install, reload and a 20s settle loop all run between the manager
    issuing a certificate and this greffer reporting what is served. A stop
    or `greffer update` in there leaves the manager holding a pending mint
    that nothing reconciles.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: _cert())
    seen: dict = {}

    def _install(greffon_id, cert):
        # What a SIGKILL between mint and report would have left behind.
        seen['debt'] = cert_renewal._unconfirmed.get('inst-1')

    monkeypatch.setattr(cert_renewal, '_install', _install)
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert 'debt' in seen and seen['debt'] is not None, (
            'the mint is outstanding at the manager with nothing owed here')
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_debt_report_stands_off_from_an_untracked_recreation(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on 17f7802: the standoff used one of the two guards.

    compose_inflight only knows about operations THIS process launched, and
    the debt path runs at the top of the first pass after a restart -- when
    that counter is empty by definition. _sidecar_settling is what covers
    the sidecar still coming up, so without it the retry reports a
    transient serial and clears the debt on it.
    """
    cert_renewal._unconfirmed['inst-1'] = OLD
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (None, None))
    monkeypatch.setattr('app.routers.controller.compose_inflight',
                        lambda g: False)
    monkeypatch.setattr(cert_renewal, '_sidecar_settling', lambda g: True)
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert wired['report'] == []
        assert 'inst-1' in cert_renewal._unconfirmed
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_mint_whose_response_is_lost_still_leaves_a_debt(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2 on afbf7e1: the last loss window is inside _mint itself.

    The manager can commit the issuance and the response still be dropped or
    arrive unparseable. Locally that is indistinguishable from a mint that
    never happened -- but the manager is holding a pending mint, later
    attempts collect its outstanding-mint refusal, and the served
    certificate expires while nothing reconciles it.
    """
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (OLD, _expiring(1)))

    def _mint(*a, **kw):
        raise cert_renewal.requests.RequestException('response dropped')

    monkeypatch.setattr(cert_renewal, '_mint', _mint)
    try:
        with pytest.raises(cert_renewal.requests.RequestException):
            cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert 'inst-1' in cert_renewal._unconfirmed, (
            'the manager may hold a pending mint and nothing here owes a report')
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_definitive_refusal_leaves_no_speculative_debt(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal issued nothing, so a debt invented here would have the next
    pass report a serial against a pending mint that does not exist."""
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: None)
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert 'inst-1' not in cert_renewal._unconfirmed
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


def test_a_refusal_does_not_discard_a_debt_that_was_already_owed(
    settings: Settings, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing debt is a real one. Clearing it on an unrelated refusal
    would drop a report we still owe, which is how a serial goes unreported
    for the thirty days until the certificate is due again."""
    monkeypatch.setattr(cert_renewal, '_served_certificate',
                        lambda h, p: (OLD, _expiring(1)))
    monkeypatch.setattr(cert_renewal, '_mint', lambda s, t, g: None)
    # The debt path RUNS -- it must reach the mint for the branch under test
    # to execute at all. `wired` stubs _report, which therefore does not
    # clear, so the debt is still owed when the refusal lands. (Deferring the
    # debt path instead returns at the settle guard, well before the mint,
    # and the test passes without ever reaching the code it names.)
    monkeypatch.setattr('app.routers.controller.compose_inflight',
                        lambda g: False)
    monkeypatch.setattr(cert_renewal, '_sidecar_settling', lambda g: False)
    cert_renewal._unconfirmed['inst-1'] = OLD
    try:
        cert_renewal.renew_one(settings, 'tok', 'inst-1')
        assert cert_renewal._unconfirmed.get('inst-1') == OLD
    finally:
        cert_renewal._unconfirmed.pop('inst-1', None)


# ---- the restore -> start handoff -----------------------------------------

def test_renewal_stands_off_a_pending_handoff_without_touching_the_lock(
        settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore releases the per-instance lock so the manager's chained start can
    take it. That start's acquire is NON-BLOCKING, so renewal must not touch the
    lock at all during the handoff -- even a momentary grab 409s the start and
    lands the restore as ``restored_start_failed``.

    So the check has to sit BEFORE the acquire, not inside the lock like
    compose_inflight and sidecar_settling.
    """
    from app import backup

    acquires = []

    class _Spy:
        def __init__(self):
            self._inner = __import__("threading").Lock()

        def acquire(self, *a, **kw):
            acquires.append((a, kw))
            return self._inner.acquire(*a, **kw)

        def release(self):
            return self._inner.release()

    monkeypatch.setattr(backup, "_instance_lock", lambda _id: _Spy())
    monkeypatch.setattr(cert_renewal, "_renew_locked",
                        lambda *a, **k: pytest.fail("renewal ran mid-handoff"))

    backup.reserve_handoff("hx")
    try:
        out = cert_renewal.renew_one(settings, "tok", "hx")
    finally:
        backup.clear_handoff("hx")

    assert out.status == cert_renewal.SKIPPED
    assert out.reason == "handoff"
    assert acquires == [], "renewal took the lock the chained start needs"


def test_the_handoff_standoff_also_covers_the_debt_probe(
        settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard has to sit above the DEBT block, not merely above the renewal
    acquire.

    An instance that owes the manager a confirmation goes through the debt path
    first, and that path takes the SAME per-instance lock for its probe. A guard
    placed after it -- which is where this one first went -- is bypassed entirely
    for exactly those instances, and the race is back.
    """
    from app import backup

    acquires = []

    class _Spy:
        def __init__(self):
            self._inner = __import__("threading").Lock()

        def acquire(self, *a, **kw):
            acquires.append((a, kw))
            return self._inner.acquire(*a, **kw)

        def release(self):
            return self._inner.release()

    monkeypatch.setattr(backup, "_instance_lock", lambda _id: _Spy())
    monkeypatch.setattr(cert_renewal, "_renew_locked",
                        lambda *a, **k: pytest.fail("renewal ran mid-handoff"))
    monkeypatch.setattr(cert_renewal, "_served_certificate",
                        lambda *a, **k: pytest.fail("debt probe ran mid-handoff"))

    cert_renewal._unconfirmed["hz"] = "serial-owed"
    backup.reserve_handoff("hz")
    try:
        out = cert_renewal.renew_one(settings, "tok", "hz")
    finally:
        backup.clear_handoff("hz")
        cert_renewal._unconfirmed.pop("hz", None)

    assert out.status == cert_renewal.SKIPPED
    assert out.reason == "handoff"
    assert acquires == [], (
        "the debt probe took the lock the chained start needs -- the guard is "
        "below the debt block")


def test_a_handoff_skip_is_not_a_deferral(settings: Settings) -> None:
    """Deliberately NOT in _TRANSIENT_SKIPS.

    A deferral means "come back soon, there is work here", and there is not. NOT
    because the start mints a certificate for us -- the manager only records a
    mint after the greffer returns 200, so a start we 409 delivers nothing. The
    reason is the arithmetic: one 90s window against a 6h cadence and a 30-day
    certificate cannot expire anything. Counting it would also drop the node's
    next tick from 6h to the 120s deferred-retry cadence after every restore.
    """
    assert "handoff" not in cert_renewal._TRANSIENT_SKIPS
    assert "instance_busy" in cert_renewal._TRANSIENT_SKIPS   # calibration


def test_a_control_op_consumes_the_handoff_reservation() -> None:
    """The awaited op arriving ends the standoff immediately, rather than leaving
    renewal blocked for the rest of the window."""
    from app import backup
    from app.routers import controller

    backup.reserve_handoff("hy")
    assert backup.handoff_pending("hy") is True

    lock = backup._instance_lock("hy")

    @controller._serialize_instance_op
    def _op(payload, request):
        return "ran"

    assert _op(SimpleNamespace(id="hy"), None) == "ran"
    assert backup.handoff_pending("hy") is False, (
        "the reservation outlived the op it was held for")
    assert not lock.locked()
