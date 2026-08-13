"""Instance upstream-certificate renewal worker.

Per-instance upstream certs are minted once, when the instance starts, with a
30-day TTL. Nothing renewed them, so on day 31 the sidecar serves an expired
certificate and every request to that app answers 502. That is the defect this
worker exists to close; the manager half (mint + report) shipped in
greffon/manager#199.

Why this reloads the sidecar itself, rather than dropping a file and trusting
a watcher: the INSTANCE sidecar is the stock ``nginx:1.20.2-alpine-perl``
image (``apps/utils/docker/compose.py``), not the greffer's own nginx image.
It has no ``nginx.sh``, no inotify watch, no reload path of any kind. A
certificate written into a running sidecar is simply never read again. (The
greffer's OWN nginx does have a watcher, and its reload was separately broken
-- greffon/greffer#68 -- but that is a different container and a different
certificate.)

Reload, verify, and only then restart. ``nginx -s reload`` is zero-downtime
but signals a master that may be unhealthy, and it reports success for a
signal that changed nothing -- the exact failure mode that hid the greffer
nginx bug for four years. So the worker asks the sidecar what it is ACTUALLY
serving afterwards, and escalates to a container restart only when the served
serial still does not match. Reporting happens after that check, so the
manager records what is being served rather than what we hoped.

Both outcomes are reported. ``instance_cert_installed`` is a report, not a
confirmation: it compares the serial against its own pending record, and its
mismatch branch is what retires a dead mint into the prunable orphan ledger and
starts a cooldown. Reporting only successes would leave every failed mint live,
uncounted and unrevokable, and re-mint another one on the next tick.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import ssl
import time

import anyio
import requests
from fastapi import FastAPI

from app.diagnostics import diag
from app.settings import Settings
from app.token import resolve_token

logger = logging.getLogger('greffer')

_HTTP_TIMEOUT_SECONDS = 30
# The sidecar is on the host's published port; a TLS handshake to read back the
# served certificate must not hang the tick.
_PROBE_TIMEOUT_SECONDS = 5


def _nginx_container(greffon_id: str) -> str:
    """Compose names the sidecar ``<project>-greffon_nginx-1``.

    The project is the instance id, which is how every other docker call in
    this codebase addresses an instance's containers.
    """
    return f'{greffon_id}-greffon_nginx-1'


def _served_certificate(host: str, port: int) -> tuple[str | None, dt.datetime | None]:
    """(serial, notAfter) of the certificate the sidecar is serving right now.

    Deliberately a real TLS handshake against the listening socket, not a read
    of ``pem.crt``. The file is what we wrote; this is what nginx loaded. Those
    differ for the entire window this worker exists to close, and only the
    second one is what a user's browser sees.

    Verification is off on purpose: the question is "which certificate is being
    presented", not "is it trusted". A renewal that installs a cert this
    greffer cannot itself validate is still a renewal, and refusing to look
    would leave the mismatch undetected.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with ctx.wrap_socket(
            __import__('socket').create_connection((host, port), _PROBE_TIMEOUT_SECONDS),
            server_hostname=host,
        ) as sock:
            peer = sock.getpeercert(binary_form=False)
            der = sock.getpeercert(binary_form=True)
    except OSError as exc:
        logger.warning('cert_renewal_probe_failed host=%s port=%s: %r', host, port, exc)
        return None, None

    serial = None
    not_after = None
    if peer:
        # `serialNumber` from getpeercert() is an uppercase hex string; the
        # manager records the serial as produced by the CA. Normalised at the
        # comparison site rather than here, so the raw value stays loggable.
        serial = peer.get('serialNumber')
        raw = peer.get('notAfter')
        if raw:
            try:
                not_after = dt.datetime.strptime(raw, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=dt.timezone.utc)
            except ValueError:
                logger.warning('cert_renewal_bad_not_after value=%r', raw)
    if serial is None and der:
        # A peer dict is empty when verification is disabled on some builds;
        # fall back to parsing the DER we were handed.
        try:
            from cryptography import x509

            serial = format(x509.load_der_x509_certificate(der).serial_number, 'x')
        except Exception:  # diagnostic path, never fatal
            logger.warning('cert_renewal_der_parse_failed', exc_info=True)
    return serial, not_after


def _same_serial(a: str | None, b: str | None) -> bool:
    """Serial comparison that survives formatting differences.

    OpenSSL hands back uppercase hex, the manager stores whatever the CA
    produced, and one side may carry a leading zero. Comparing the raw strings
    made a correct renewal look like a mismatch and triggered a pointless
    container restart.
    """
    if not a or not b:
        return False
    return a.strip().lower().lstrip('0') == b.strip().lower().lstrip('0')


def _mint(settings: Settings, token: str, greffon_id: str) -> dict | None:
    """Ask the manager for a renewal certificate. None when there is nothing to do."""
    res = requests.post(
        f'{settings.greffon_base_server}/api/greffer/instances/{greffon_id}/cert/',
        json={},
        headers={'X-Greffer-Token': token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    if res.status_code == 404:
        # CERT_RENEWAL_ENABLED is off on this manager, or the instance is gone.
        # Both mean "do not renew", and neither is an error worth alarming on:
        # a deployment that has not opted in must not fill its logs.
        logger.debug('cert_renewal_unavailable instance=%s', greffon_id)
        return None
    if res.status_code == 429:
        # Either the per-greffer 30/h cap or this instance's mismatch cooldown.
        # Both mean "stop asking", so arm the backoff here rather than in the
        # caller: a node at its cap that keeps asking every tick is what starves
        # its healthy instances into 429s alongside the stuck one.
        logger.info('cert_renewal_rate_limited instance=%s', greffon_id)
        _note_failure(greffon_id, settings.cert_renewal_interval)
        return None
    if res.status_code != 200:
        logger.warning(
            'cert_renewal_mint_failed instance=%s status=%s body=%s',
            greffon_id,
            res.status_code,
            res.text[:200],
        )
        return None
    return res.json()


def _install(greffon_id: str, cert: dict) -> None:
    """Write the new material into the sidecar, key FIRST.

    Order matters even without a watcher: nginx re-reads both files on reload,
    and a reload racing a half-written pair would load a certificate against
    the previous key. Writing the key first means the worst interleaving loads
    the OLD pair, which still serves.
    """
    from apps.utils.docker.base import copy_file_into_container

    container = _nginx_container(greffon_id)
    copy_file_into_container(container, '/etc/nginx', 'cert.key', cert['private_key'])
    copy_file_into_container(container, '/etc/nginx', 'pem.crt', cert['certificate'])


def _reload(greffon_id: str) -> bool:
    from apps.utils.docker.exec_op import exec_in_container

    result = exec_in_container(_nginx_container(greffon_id), ['nginx', '-s', 'reload'])
    if not result.ok:
        logger.warning(
            'cert_renewal_reload_failed instance=%s exit=%s err=%s',
            greffon_id,
            result.exit_code,
            (result.stderr or b'')[:200],
        )
        return False
    # NOT proof on its own. `nginx -s reload` exits 0 for a signal it
    # delivered, not for a configuration the master actually adopted -- the
    # caller verifies by handshake before believing this.
    return True


def _restart(greffon_id: str) -> bool:
    """Restart just the sidecar container.

    Through the docker client directly rather than compose: ``compose.start`` /
    ``compose.stop`` take a whole ``greffon_info`` dict, and bouncing the app's
    own containers to reload a proxy certificate would turn a brief proxy blip
    into a real outage for a stateful greffon.
    """
    from apps.utils.docker.base import client

    logger.warning('cert_renewal_restarting_sidecar instance=%s', greffon_id)
    try:
        client.containers.get(_nginx_container(greffon_id)).restart(timeout=10)
        return True
    except Exception:  # last resort; never kill the tick
        logger.exception('cert_renewal_restart_failed instance=%s', greffon_id)
        return False


def _report(settings: Settings, token: str, greffon_id: str,
            served_serial: str | None) -> None:
    """Tell the manager which serial is actually being served.

    A REPORT, not a confirmation, and therefore sent on BOTH outcomes with
    whatever the handshake found. ``instance_cert_installed`` compares the value
    against its own pending record and branches: a match confirms and supersedes,
    a mismatch retires the dead mint into the prunable orphan ledger and starts
    ``CERT_RENEWAL_MISMATCH_COOLDOWN_MINUTES``.

    So staying silent after a failed install is the worse option, not the safer
    one. It leaves a live 30-day SERVER_AUTH cert the manager cannot revoke,
    count or prune, keeps the gate open, and re-mints another one next tick.
    ``None`` is a legitimate value here (the sidecar answered nothing): the
    manager reads an unparseable serial as a mismatch, which is the truth.

    Sent AFTER the verification handshake, never before -- reporting the
    intended serial would confirm a certificate the sidecar may not have loaded.
    """
    try:
        res = requests.post(
            f'{settings.greffon_base_server}/api/greffer/instances/{greffon_id}/cert-installed/',
            json={'served_serial': served_serial},
            headers={'X-Greffer-Token': token},
            verify=settings.greffer_ssl_verify,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        if res.status_code == 409:
            # serial_mismatch / no_pending_mint / stale. The manager received
            # the report and acted on it; the renewal simply did not land. Not
            # a transport failure, and not retryable -- the pending is gone.
            diag('cert_renewal_report_rejected', instance=greffon_id,
                 body=res.text[:120], level=logging.WARNING)
        elif res.status_code != 200:
            logger.warning(
                'cert_renewal_report_failed instance=%s status=%s body=%s',
                greffon_id,
                res.status_code,
                res.text[:200],
            )
    except requests.RequestException:
        # The certificate IS installed and serving; only the bookkeeping call
        # failed. The manager's reaper reconciles an unconfirmed pending record,
        # so losing this is recoverable and must not abort the tick.
        logger.warning('cert_renewal_report_error instance=%s', greffon_id, exc_info=True)


def _sidecar_host_port(settings: Settings, greffon_id: str) -> int | None:
    """The host port the instance's nginx sidecar publishes.

    Read from the rendered compose the greffer already wrote for this instance
    (``<GREFFON_PATH>/<id>/docker-compose.yml``), which is the only local record
    of the allocation. Probing 127.0.0.1 on that port keeps the verification
    handshake independent of docker networking: the worker does not need to
    join the instance's internal network, and a sidecar that is not listening
    reads as "cannot verify" rather than as a false success.
    """
    import yaml

    path = settings.greffon_path / greffon_id / 'docker-compose.yml'
    try:
        rendered = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        logger.warning('cert_renewal_no_compose instance=%s path=%s', greffon_id, path)
        return None
    ports = ((rendered or {}).get('services', {}).get('greffon_nginx', {}) or {}).get('ports') or []
    for mapping in ports:
        # "45347:8000" -- host:container. Only the host side is reachable here.
        host = str(mapping).split(':')[0].strip()
        if host.isdigit():
            return int(host)
    logger.warning('cert_renewal_no_published_port instance=%s', greffon_id)
    return None


def _due_for_renewal(not_after: dt.datetime | None, window_days: int) -> bool:
    """Renew inside the window, and ALSO when the expiry is unreadable.

    An unknown expiry is treated as due rather than as fine. The failure this
    worker exists to fix is a certificate nobody renewed, so the safe default
    when we cannot tell is to ask the manager -- which is rate-limited and
    idempotent, and answers 404 when renewal is off. Treating unknown as
    healthy would reproduce the original bug for exactly the instances whose
    state we cannot read.
    """
    if not_after is None:
        return True
    remaining = not_after - dt.datetime.now(dt.timezone.utc)
    return remaining <= dt.timedelta(days=window_days)


# Per-instance backoff, keyed on greffon id -> (consecutive failures,
# monotonic deadline). In-process on purpose: the greffer runs ``--workers 1``
# by design (see CLAUDE.md), so this worker is the only thing renewing on this
# node and a shared store would buy nothing. A greffer restart clears it, which
# is acceptable -- a restart also re-creates the sidecars, so the failure the
# backoff was suppressing may genuinely be gone.
_renewal_backoff: dict[str, tuple[int, float]] = {}

# A failing instance must still get several attempts inside its renewal window,
# or the backoff itself becomes the expiry. Capped at a day: with the default
# 7-day window that leaves ~7 tries, while a permanently stuck instance mints
# ~8 certs over the window instead of ~28.
_BACKOFF_CAP_SECONDS = 24 * 60 * 60


def _in_backoff(greffon_id: str) -> bool:
    entry = _renewal_backoff.get(greffon_id)
    return entry is not None and time.monotonic() < entry[1]


def _note_failure(greffon_id: str, interval: int) -> None:
    """Back off exponentially after a renewal that did not land.

    The failure this guards is not wasted work, it is issuance. Every attempt
    that reaches the mint burns a live 30-day SERVER_AUTH certificate out of a
    30/hour per-greffer budget and appends it to the orphan ledger. Retrying a
    stuck instance on the fixed tick crowds the node's healthy instances out of
    that budget and 429s them toward the very expiry this worker prevents.
    """
    failures = _renewal_backoff.get(greffon_id, (0, 0.0))[0] + 1
    delay = min(interval * (2 ** (failures - 1)), _BACKOFF_CAP_SECONDS)
    _renewal_backoff[greffon_id] = (failures, time.monotonic() + delay)
    diag('cert_renewal_backoff', instance=greffon_id, failures=failures,
         delay_seconds=int(delay), level=logging.WARNING)


def _clear_backoff(greffon_id: str) -> None:
    _renewal_backoff.pop(greffon_id, None)


def renew_one(settings: Settings, token: str, greffon_id: str) -> str | None:
    """Renew one instance if it is due. Returns the serial now being served.

    The order is the whole design: install, reload, ASK WHAT IS SERVED, restart
    only on a mismatch, and report last. Every step short of the handshake can
    succeed while the sidecar still serves the old certificate.
    """
    if _in_backoff(greffon_id):
        diag('cert_renewal_skipped', instance=greffon_id, reason='backoff')
        return None

    port = _sidecar_host_port(settings, greffon_id)
    if port is None:
        diag('cert_renewal_skipped', instance=greffon_id, reason='no_published_port')
        return None

    before_serial, before_expiry = _served_certificate('127.0.0.1', port)
    if not _due_for_renewal(before_expiry, settings.cert_renewal_window_days):
        diag('cert_renewal_skipped', instance=greffon_id, reason='not_due')
        return before_serial

    cert = _mint(settings, token, greffon_id)
    if cert is None:
        # 404 (renewal off / instance gone) or 429 (capped or cooling down).
        # Nothing was issued, so there is nothing to install and nothing to
        # report; _mint has already armed the backoff for the 429 case.
        return before_serial
    wanted = cert.get('serial_number')

    _install(greffon_id, cert)
    _reload(greffon_id)

    served, _ = _served_certificate('127.0.0.1', port)
    if not _same_serial(served, wanted):
        # The reload did not take. This is the case the whole worker is shaped
        # around: on the stock sidecar image nothing else would ever notice.
        logger.warning(
            'cert_renewal_reload_did_not_take instance=%s wanted=%s served=%s',
            greffon_id, wanted, served,
        )
        if _restart(greffon_id):
            served, _ = _served_certificate('127.0.0.1', port)

    # Reported either way, with what the handshake actually found. The manager
    # branches on the comparison itself: a match confirms and supersedes the old
    # serial, a mismatch retires our dead mint into the orphan ledger and starts
    # a cooldown. Reporting only successes would leave every failed mint live,
    # uncounted and unrevokable, and re-mint it on the next tick.
    _report(settings, token, greffon_id, served)

    if _same_serial(served, wanted):
        diag('cert_renewal_outcome', instance=greffon_id, outcome='renewed',
             serial=served)
        _clear_backoff(greffon_id)
    else:
        diag('cert_renewal_outcome', instance=greffon_id, outcome='not_served',
             wanted=wanted, served=served, level=logging.ERROR)
        _note_failure(greffon_id, settings.cert_renewal_interval)
    return served


def renew_all(settings: Settings) -> None:
    """One tick over every instance on this greffer.

    Per-instance failures are contained: one unreachable sidecar must not stop
    the others from renewing, because they expire on their own schedules and a
    single bad instance would otherwise take the whole fleet down with it.
    """
    from app.workers.status_collect import collect_status_map

    token = resolve_token(settings)
    considered = errors = 0
    for greffon_id, status in collect_status_map(settings).items():
        if status != 'running':
            # A stopped instance has no sidecar to reload, and its certificate
            # is re-minted at start anyway.
            continue
        considered += 1
        try:
            renew_one(settings, token, greffon_id)
        except Exception:  # one instance must not end the tick
            errors += 1
            logger.exception('cert_renewal_instance_error instance=%s', greffon_id)
    # One line per tick, so "did renewal run at all" is answerable from the
    # logs. A worker that silently stopped ticking looks exactly like a fleet
    # with nothing due, which is the state this whole feature exists to detect.
    diag('cert_renewal_tick', considered=considered, errors=errors,
         backing_off=len(_renewal_backoff))


async def cert_renewal_worker(app: FastAPI) -> None:
    """Periodically renew per-instance upstream certificates.

    Sleep-first, matching the other workers: instance certificates are minted
    at start, so nothing is due in the seconds after boot, and a fleet of
    greffers restarting together must not stampede the manager's mint endpoint.
    """
    settings: Settings = app.state.settings
    try:
        while True:
            await asyncio.sleep(settings.cert_renewal_interval)
            try:
                await anyio.to_thread.run_sync(
                    renew_all, settings, abandon_on_cancel=True
                )
            except Exception:  # the loop outlives any one tick
                logger.exception('cert_renewal_tick_failed')
    except asyncio.CancelledError:
        logger.info('cert renewal worker stopped')
        raise
