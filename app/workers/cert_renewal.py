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
import socket
import ssl
import sys
import threading
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
# How long the sidecar gets to actually start serving the new certificate after
# a reload or a restart before the mismatch is believed.
_SETTLE_SECONDS = 20
_SETTLE_POLL_SECONDS = 0.5
# How many consecutive instances may read back nothing before the tick decides
# the fault is the probe path rather than the fleet.
_BLIND_STREAK_ABORT = 3
# A dropped confirmation is terminal (the next tick reads not-due and never
# reports again), so 5xx and transport failures are retried here.
_REPORT_ATTEMPTS = 3
_REPORT_RETRY_SECONDS = 2


class _NodeRateLimited(Exception):
    """The manager refused on the per-GREFFER cap, not on this instance."""


# Consecutive instances whose sidecar answered nothing. Reset by any readable
# one, so it only climbs when the fault is shared.
_blind_streak = 0

# One renewal pass at a time on this node. The periodic tick and the operator's
# /renew-certs/ call both land here in the same process, and two passes would
# interleave over the shared blind-streak counter and the backoff map, each
# undoing the other's accounting -- while racing each other for the same
# per-instance locks and the same per-greffer mint budget. There is no reason
# to run two.
_pass_lock = threading.Lock()


class RenewalAlreadyRunning(Exception):
    """A renewal pass is already in progress on this node."""


def _nginx_container(greffon_id: str) -> str:
    """Compose names the sidecar ``<project>-greffon_nginx-1``.

    The project is the instance id, which is how every other docker call in
    this codebase addresses an instance's containers.
    """
    return f'{greffon_id}-greffon_nginx-1'


def _peer_info(sock: ssl.SSLSocket) -> dict | None:
    """The peer certificate's fields, without validating it.

    ``getpeercert()`` is empty under CERT_NONE, and the greffer does not ship
    ``cryptography`` (the image is deliberately slim), so the chain accessor is
    the only way to read an expiry off a certificate we are explicitly not
    trusting.

    It has to be the ``_sslobj`` one. The public ``SSLSocket`` method added in
    3.13 returns a list of raw DER BYTES, which is useless without a parser we
    do not have; the identically-named method on the underlying object returns
    ``_ssl.Certificate`` objects that decode themselves. Verified on 3.11 and
    3.14 -- 3.11 is this project's floor.
    """
    for holder in (getattr(sock, '_sslobj', None), sock):
        getter = getattr(holder, 'get_unverified_chain', None)
        if getter is None:
            continue
        chain = getter()
        if not chain:
            return None
        info = getattr(chain[0], 'get_info', None)
        if info is not None:
            return info()
    logger.error('cert_renewal_no_chain_api python=%s', sys.version)
    return None


def _served_certificate(host: str, port: int) -> tuple[str | None, dt.datetime | None]:
    """(serial, notAfter) of the certificate the sidecar is serving right now.

    Deliberately a real TLS handshake against the listening socket, not a read
    of ``pem.crt``. The file is what we wrote; this is what nginx loaded. Those
    differ for the entire window this worker exists to close, and only the
    second one is what a user's browser sees.

    Verification is off on purpose: the question is "which certificate is being
    presented", not "is it trusted". A renewal that installs a cert this greffer
    cannot itself validate is still a renewal, and refusing to look would leave
    the mismatch undetected.

    Read through ``get_unverified_chain``, NOT ``getpeercert()``. Under
    ``CERT_NONE`` CPython returns an EMPTY dict from ``getpeercert()`` --
    always, on every build, by design -- so an earlier revision that read
    ``notAfter`` from there saw None for every instance forever, which silently
    made every instance permanently "due" and turned the not-due short-circuit
    into dead code.
    """
    raw = sock = None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), _PROBE_TIMEOUT_SECONDS)
        # Bound the handshake too. create_connection's timeout covers the TCP
        # connect only; a peer that accepts and then never speaks TLS would
        # otherwise park this worker thread for the life of the process.
        raw.settimeout(_PROBE_TIMEOUT_SECONDS)
        sock = ctx.wrap_socket(raw, server_hostname=host)
        info = _peer_info(sock)
    except Exception as exc:  # noqa: BLE001 -- any failure here means "cannot tell"
        logger.debug('cert_renewal_probe_failed host=%s port=%s: %r', host, port, exc)
        return None, None
    finally:
        # wrap_socket self-closes the underlying socket only for OSError and
        # ValueError, so anything else it raises would leak the fd every tick.
        target = sock or raw
        if target is not None:
            target.close()
    if info is None:
        return None, None
    # Uppercase hex here, lowercase from the CA. Normalised at the comparison
    # site rather than now, so the raw value stays loggable as served.
    serial = info.get('serialNumber')
    raw = info.get('notAfter')
    not_after = None
    if raw:
        try:
            not_after = dt.datetime.strptime(
                raw, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=dt.timezone.utc)
        except ValueError:
            logger.warning('cert_renewal_bad_not_after value=%r', raw)
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
        # Two different 429s, and charging the wrong one to the wrong scope is
        # what starves a node. `renewal_cooling_down` is this instance's own
        # mismatch cooldown -- back it off. `rate_limited` is the per-greffer
        # 30/hour cap, which says nothing about THIS instance: penalising it
        # exponentially pushes healthy instances to the back of a queue they
        # were never at fault in, and the node ends up using a fraction of its
        # own budget. Raise it so the caller can stop the tick instead.
        if 'rate_limited' in res.text:
            logger.info('cert_renewal_node_rate_limited instance=%s', greffon_id)
            raise _NodeRateLimited
        logger.info('cert_renewal_cooling_down instance=%s', greffon_id)
        _note_failure(greffon_id, settings.cert_renewal_interval)
        return None
    if res.status_code != 200:
        # 409 cert_cn_would_not_match in particular can never self-resolve --
        # the greffer's address changed and the field row did not -- so
        # retrying it every tick forever is pure noise. Back off on any refusal
        # we did not specifically handle above.
        logger.warning(
            'cert_renewal_mint_failed instance=%s status=%s body=%s',
            greffon_id,
            res.status_code,
            res.text[:200],
        )
        _note_failure(greffon_id, settings.cert_renewal_interval)
        return None
    return res.json()


def _install(greffon_id: str, cert: dict) -> None:
    """Write the new key and certificate into the sidecar, in ONE archive.

    Two separate ``put_archive`` calls leave a window where /etc/nginx holds a
    NEW key against the OLD certificate. nginx keeps serving from memory, so
    nothing looks wrong -- until the sidecar next restarts for any unrelated
    reason (host reboot, dockerd restart, OOM under ``restart:
    unless-stopped``), when loading the mismatched pair fails and the container
    crash-loops into a permanent 502. That is this worker's own failure mode,
    deferred by weeks and disconnected from its cause.

    One tar is one extraction on the daemon side, which is the closest thing to
    atomic available here.
    """
    import io
    import tarfile
    import time as _time

    from apps.utils.docker.base import client

    stream = io.BytesIO()
    with tarfile.TarFile(fileobj=stream, mode='w') as tar:
        for name, content in (('cert.key', cert['private_key']),
                              ('pem.crt', cert['certificate'])):
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(_time.time())
            tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    client.containers.get(_nginx_container(greffon_id)).put_archive(
        '/etc/nginx', stream)


def _reload(greffon_id: str) -> bool:
    """SIGHUP the sidecar's nginx. False on any docker-level failure.

    ``exec_in_container`` wants a container OBJECT -- it dereferences
    ``container.id`` -- and its own ``_DOCKER_ERRORS`` does not include the
    ``AttributeError`` a bare name string produces. Passing the name aborted
    every renewal after the files were already written, with no report and no
    backoff, so the manager kept a pending record the greffer never resolved.
    """
    from apps.utils.docker.base import client
    from apps.utils.docker.exec_op import exec_in_container

    try:
        container = client.containers.get(_nginx_container(greffon_id))
        result = exec_in_container(container, ['nginx', '-s', 'reload'])
    except Exception:  # the restart escalation is the fallback
        logger.warning('cert_renewal_reload_error instance=%s', greffon_id,
                       exc_info=True)
        return False
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
    for attempt in range(_REPORT_ATTEMPTS):
        try:
            res = requests.post(
                f'{settings.greffon_base_server}/api/greffer/instances/{greffon_id}/cert-installed/',
                json={'served_serial': served_serial},
                headers={'X-Greffer-Token': token},
                verify=settings.greffer_ssl_verify,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            logger.warning('cert_renewal_report_error instance=%s attempt=%s',
                           greffon_id, attempt, exc_info=True)
            res = None
        if res is not None and res.status_code == 200:
            return
        if res is not None and res.status_code == 409:
            # serial_mismatch / no_pending_mint / stale. The manager received
            # the report and acted on it; the renewal simply did not land. Not
            # a transport failure, and not retryable -- the pending is gone.
            diag('cert_renewal_report_rejected', instance=greffon_id,
                 body=res.text[:120], level=logging.WARNING)
            return
        if res is not None and res.status_code < 500:
            logger.warning(
                'cert_renewal_report_failed instance=%s status=%s body=%s',
                greffon_id, res.status_code, res.text[:200],
            )
            return
        # 5xx or transport. Worth retrying, and it MUST be retried here: a
        # dropped confirmation is otherwise terminal. The certificate is
        # installed and serving, so the next tick reads it as not-due and never
        # reports again -- leaving the manager's cert_serial pinned to a
        # superseded value (false expiry alarms on a healthy instance), its
        # pending parked as an orphan, and the old certificate never revoked.
        # The manager's own 503 exists so the greffer re-reports.
        if attempt + 1 < _REPORT_ATTEMPTS:
            time.sleep(_REPORT_RETRY_SECONDS * (attempt + 1))
    logger.error('cert_renewal_report_unconfirmed instance=%s serial=%s',
                 greffon_id, served_serial)


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


def _await_serial(host: str, port: int, wanted: str | None) -> tuple[str | None, dt.datetime | None]:
    """Poll the handshake until it settles, rather than trusting one shot.

    Neither preceding step is synchronous with what this measures.
    ``nginx -s reload`` only delivers SIGHUP: the master then re-reads config,
    forks new workers and drains the old ones, which still hold the shared
    listening socket and still answer with the OLD certificate for a moment.
    ``container.restart()`` returns when the daemon has started the container,
    not when nginx has bound its port, so an immediate probe gets connection
    refused.

    Reading either as a mismatch is not a harmless retry: it restarts a healthy
    sidecar, then reports a failure for a renewal that worked, which makes the
    manager orphan the certificate the instance is actually serving and start a
    cooldown. So poll until the wanted serial appears, and only conclude
    otherwise once it has had time not to.
    """
    deadline = time.monotonic() + _SETTLE_SECONDS
    served, not_after = _served_certificate(host, port)
    while not _same_serial(served, wanted) and time.monotonic() < deadline:
        time.sleep(_SETTLE_POLL_SECONDS)
        served, not_after = _served_certificate(host, port)
    return served, not_after


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


def renew_one(settings: Settings, token: str, greffon_id: str,
              force: bool = False) -> str | None:
    """Renew one instance if it is due. Returns the serial now being served.

    The order is the whole design: install, reload, ASK WHAT IS SERVED, restart
    only on a mismatch, and report last. Every step short of the handshake can
    succeed while the sidecar still serves the old certificate.

    Held under the same per-instance lock as start / stop / backup / restore.
    Renewal writes a key and a certificate into a sidecar that a concurrent
    Start is simultaneously recreating and writing its OWN pair into, so
    without this the two interleave and which certificate nginx ends up serving
    is decided by timing -- including a crossed key/cert pair that fails to
    load at all. The manager sees the wreckage as ``instance_cert_renewal_orphan``
    and refuses to guess which cert is live; its comment names this
    serialization as the precondition for enabling renewal at all.

    ``force`` drops only the two LOCAL guards -- the backoff and the not-due
    check -- for an operator running ``renew_certs`` against an instance that is
    already 502ing. It grants nothing: the manager re-checks the window, the
    cooldown, the outstanding mint and the per-greffer cap on its own side, and
    a forced call that is genuinely not due just collects a 409. It does NOT
    drop the instance lock.
    """
    from app.backup import _instance_lock

    lock = _instance_lock(greffon_id)
    if not lock.acquire(blocking=False):
        # Non-blocking: a renewal is never urgent enough to queue behind a
        # restore, and holding the tick here would stall every later instance.
        # Not a failure either -- whatever holds the lock (a Start, most
        # likely) mints its own certificate anyway.
        diag('cert_renewal_skipped', instance=greffon_id, reason='instance_busy')
        return None
    try:
        return _renew_locked(settings, token, greffon_id, force)
    finally:
        lock.release()


def _renew_locked(settings: Settings, token: str, greffon_id: str,
                  force: bool) -> str | None:
    if _in_backoff(greffon_id) and not force:
        diag('cert_renewal_skipped', instance=greffon_id, reason='backoff')
        return None

    port = _sidecar_host_port(settings, greffon_id)
    if port is None:
        diag('cert_renewal_skipped', instance=greffon_id, reason='no_published_port')
        return None
    host = settings.cert_probe_host

    before_serial, before_expiry = _served_certificate(host, port)
    global _blind_streak
    _blind_streak = _blind_streak + 1 if before_serial is None else 0
    if not force and not _due_for_renewal(
            before_expiry, settings.cert_renewal_window_days):
        diag('cert_renewal_skipped', instance=greffon_id, reason='not_due')
        return before_serial

    cert = _mint(settings, token, greffon_id)
    if cert is None:
        # 404 (renewal off / instance gone), 429 (capped or cooling down), or a
        # permanent 409. Nothing was issued, so there is nothing to install and
        # nothing to report; _mint arms the backoff for the cases that warrant
        # one.
        return before_serial
    wanted = cert.get('serial_number')

    _install(greffon_id, cert)
    if _reload(greffon_id):
        served, _ = _await_serial(host, port, wanted)
    else:
        # A reload that provably never happened is a different fault from one
        # that was delivered and ignored, with a different remediation. Skip
        # the settle wait -- there is nothing to settle -- and keep the two
        # countable apart in the logs.
        diag('cert_renewal_reload_error', instance=greffon_id,
             level=logging.WARNING)
        served = None

    if served is None:
        # Cannot see the sidecar at all. NOT a mismatch: restarting a container
        # we are unable to observe turns one broken probe path -- a firewall
        # rule, a greffer started without the host-gateway alias, a sidecar
        # mid-restart -- into a bounce of every healthy instance on the node,
        # every tick, forever. Report the truth and back off instead.
        logger.warning('cert_renewal_unobservable instance=%s port=%s',
                       greffon_id, port)
    elif not _same_serial(served, wanted):
        # The reload was delivered and the sidecar has had _SETTLE_SECONDS to
        # adopt it. THIS is the case the whole worker is shaped around: on the
        # stock sidecar image nothing else would ever notice.
        logger.warning(
            'cert_renewal_reload_did_not_take instance=%s wanted=%s served=%s',
            greffon_id, wanted, served,
        )
        if _restart(greffon_id):
            served, _ = _await_serial(host, port, wanted)

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


def renew_all(settings: Settings, only: str | None = None,
              force: bool = False) -> int:
    """One tick over every instance on this greffer. Returns the error count.

    Per-instance failures are contained: one unreachable sidecar must not stop
    the others from renewing, because they expire on their own schedules and a
    single bad instance would otherwise take the whole fleet down with it.
    """
    from app.workers.status_collect import collect_status_map

    if not _pass_lock.acquire(blocking=False):
        raise RenewalAlreadyRunning
    try:
        return _renew_all_locked(settings, collect_status_map, only, force)
    finally:
        _pass_lock.release()


def _renew_all_locked(settings, collect_status_map, only, force) -> int:
    token = resolve_token(settings)
    considered = errors = 0
    global _blind_streak
    _blind_streak = 0
    statuses = collect_status_map(settings)
    for greffon_id, status in statuses.items():
        if only is not None and greffon_id != only:
            continue
        if status != 'running':
            # A stopped instance has no sidecar to reload, and its certificate
            # is re-minted at start anyway.
            continue
        considered += 1
        try:
            renew_one(settings, token, greffon_id, force=force)
        except _NodeRateLimited:
            # The node hit the manager's per-greffer cap. Every remaining
            # instance would collect the same refusal, so stopping here is not
            # giving up: it leaves the rest un-penalised and due again next
            # tick, which converges far faster than backing each of them off
            # exponentially for a limit none of them caused.
            diag('cert_renewal_tick_capped', considered=considered,
                 level=logging.WARNING)
            break
        except Exception:  # one instance must not end the tick
            errors += 1
            # Armed HERE too, not only on a clean mismatch. A sidecar that has
            # been deleted, or a manager that is unreachable, raises out of
            # _mint / _install rather than returning -- and those are the
            # failures most likely to repeat. Without this they retry at the
            # full tick rate forever, each one burning a fresh 30-day mint out
            # of the budget the backoff exists to protect.
            _note_failure(greffon_id, settings.cert_renewal_interval)
            logger.exception('cert_renewal_instance_error instance=%s', greffon_id)
        if _blind_streak >= _BLIND_STREAK_ABORT:
            # Three sidecars in a row answering nothing is a statement about
            # this greffer's probe path, not about three certificates. Carrying
            # on would mint, fail to verify, and back off every instance on the
            # node in a single tick.
            diag('cert_renewal_probe_path_broken', considered=considered,
                 level=logging.ERROR)
            break
    # One line per tick, so "did renewal run at all" is answerable from the
    # logs. A worker that silently stopped ticking looks exactly like a fleet
    # with nothing due, which is the state this whole feature exists to detect.
    # Drop backoff entries for instances this node no longer runs. Otherwise a
    # decommissioned or migrated-away instance keeps its entry for the life of
    # the process, and `backing_off` -- the one number that answers "is renewal
    # healthy" -- drifts upward forever on instances that do not exist.
    for stale in set(_renewal_backoff) - set(statuses):
        _renewal_backoff.pop(stale, None)
    diag('cert_renewal_tick', considered=considered, errors=errors,
         backing_off=len(_renewal_backoff))
    return errors


async def cert_renewal_worker(app: FastAPI) -> None:
    """Periodically renew per-instance upstream certificates.

    Sleep-first, matching the other workers: instance certificates are minted
    at start, so nothing is due in the seconds after boot, and a fleet of
    greffers restarting together must not stampede the manager's mint endpoint.
    """
    settings: Settings = app.state.settings
    if not settings.cert_renewal_enabled:
        # A per-node off switch. The manager's CERT_RENEWAL_ENABLED is
        # fleet-wide, so without this an operator seeing one node misbehave can
        # only stop renewal everywhere.
        logger.info('cert renewal worker disabled by CERT_RENEWAL_ENABLED')
        return
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
