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
    yield
    cert_renewal._renewal_backoff.clear()
    cert_renewal._unconfirmed.clear()


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
    assert deadline - time.monotonic() <= cert_renewal._BACKOFF_CAP_SECONDS


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


def test_a_manager_with_renewal_off_is_not_an_error(settings: Settings, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """404 means "do not renew", and must leave the instance untouched."""
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
        assert cert_renewal.renew_all(settings, 'tok') == 2


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

    assert result == cert_renewal.Outcome(cert_renewal.SKIPPED, None), (
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
        got = {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers()}
    assert set(got) == {'cert.key', 'pem.crt'}
    assert 'PRIVATE KEY' in got['cert.key'], 'key and certificate are swapped'
    assert 'CERTIFICATE' in got['pem.crt'], 'key and certificate are swapped'


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
    fleet = {f'i{n}': 'running' for n in range(8)}

    with patch('app.workers.status_collect.collect_status_map', return_value=fleet):
        cert_renewal.renew_all(settings, 'tok')

    assert len(minted) == cert_renewal._BLIND_STREAK_ABORT, (
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
        assert cert_renewal.renew_all(settings, 'tok') == 1


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
