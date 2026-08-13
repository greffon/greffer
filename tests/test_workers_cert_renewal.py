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

    def _one(s, t, greffon_id):
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
    monkeypatch.setattr(cert_renewal, 'renew_one', lambda s, t, g: seen.append(g))
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
