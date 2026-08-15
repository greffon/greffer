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
import json
import logging
import os
import random
import socket
import ssl
import sys
import threading
import time
from typing import NamedTuple

import anyio
import requests
from fastapi import FastAPI

from app.diagnostics import diag
from app.settings import CERT_RENEWAL_BACKOFF_CAP_SECONDS, Settings

logger = logging.getLogger('greffer')

_HTTP_TIMEOUT_SECONDS = 30
# The FIRST pass runs a few jittered minutes after boot, not a full interval
# later. "Certificates were just minted" is true of an INSTANCE start; a
# greffer restart leaves running sidecars serving whatever they already had,
# including certificates that expired while the process was down. Publishing an
# image and having clients run `greffer update` is how this feature reaches a
# node at all, so a full-interval first sleep means the 502s it exists to fix
# survive the fix by six hours. Jittered because a fleet restarting together
# would otherwise mint in lockstep against one manager.
_STARTUP_JITTER_SECONDS = 300
# Come back just past the compose settle window when a pass deferred work.
# The reboot case is the one that matters: an instance sidecar and the greffer
# start together, so the early pass can land INSIDE the settle window and be
# thrown away -- and the certificate it would have renewed survives the reboot
# on its volume, already expired.
_DEFERRED_RETRY_SECONDS = 120
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
# How long after a sidecar starts we assume a compose child may still be
# working on it. compose.start/stop are Popen and the controller's lock is
# released when the HANDLER returns, not when compose exits.
_COMPOSE_SETTLE_SECONDS = 90
# After stopping on the manager's per-greffer cap, come back in an hour rather
# than a full interval: the cap refills hourly, so waiting six hours spends a
# sixth of the available budget while instances are expiring.
_CAPPED_RETRY_SECONDS = 60 * 60


class _NodeRateLimited(Exception):
    """The manager refused on the per-GREFFER cap, not on this instance."""


# Consecutive instances whose sidecar answered nothing. Reset by any readable
# one, so it only climbs when the fault is shared.
_blind_streak = 0

# Certificates that ARE serving but whose confirmation never reached the
# manager. Retried at the top of the next pass: without this a dropped
# confirmation is terminal, because the next tick reads the new certificate as
# not-due and never reports again -- leaving the manager alarming on a healthy
# instance, its pending parked as an orphan, and the superseded certificate
# unrevoked.
#
# Persisted, because the window it covers is a manager outage and the obvious
# operator response to one is to restart or update the greffer -- which would
# drop exactly the debts accumulated during it. `greffer update` recreates this
# process by design, so an in-process-only ledger loses the record precisely
# when it is most likely to hold anything.
_unconfirmed: dict[str, str] = {}
_UNCONFIRMED_FILE = '.cert-unconfirmed.json'


def _unconfirmed_path(settings: Settings):
    return settings.greffon_path / _UNCONFIRMED_FILE


def _load_unconfirmed(settings: Settings) -> None:
    """Read the owed-confirmation ledger from disk, once per pass.

    Best-effort in both directions: a missing, unreadable or corrupt file just
    means no debts, never a failed pass. The cost of losing it is stale
    bookkeeping on the manager, not a broken instance.
    """
    try:
        raw = _unconfirmed_path(settings).read_text(encoding='utf-8')
    except (OSError, AttributeError):
        return
    try:
        stored = json.loads(raw)
    except ValueError:
        logger.warning('cert_renewal_unconfirmed_unreadable')
        return
    if isinstance(stored, dict):
        for k, v in stored.items():
            if isinstance(k, str) and isinstance(v, str):
                _unconfirmed.setdefault(k, v)


def _clear_debt(settings: Settings, greffon_id: str) -> None:
    if _unconfirmed.pop(greffon_id, None) is not None:
        _save_unconfirmed(settings)


def _save_unconfirmed(settings: Settings) -> None:
    """Write the ledger atomically.

    ``write_text`` truncates and then writes, and the event this ledger exists
    to survive -- a kill during `greffer update`, a full volume -- lands inside
    that window and leaves a truncated file that reads back as ZERO debts. It
    is now rewritten once per failed report, so a manager outage across a large
    fleet opens one truncate window per instance rather than one per pass.
    """
    path = _unconfirmed_path(settings)
    try:
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(_unconfirmed), encoding='utf-8')
        os.replace(tmp, path)
    except (OSError, AttributeError, TypeError):
        # A read-only or full volume must not fail a renewal that worked.
        logger.warning('cert_renewal_unconfirmed_unwritable', exc_info=True)

# One renewal pass at a time on this node. The periodic tick and the operator's
# /renew-certs/ call both land here in the same process, and two passes would
# interleave over the shared blind-streak counter and the backoff map, each
# undoing the other's accounting -- while racing each other for the same
# per-instance locks and the same per-greffer mint budget. There is no reason
# to run two.
_pass_lock = threading.Lock()

# Set when the worker task is cancelled, so an in-flight pass stops between
# instances. anyio's worker threads are NOT daemons, so `abandon_on_cancel`
# stops the event loop WAITING on the pass without stopping the pass -- the
# interpreter then joins it at exit. The watchdog self-heals by SIGTERMing its
# own process, and nothing external SIGKILLs it, so a pass still grinding
# through a hung docker daemon (60s SDK timeout per call, per instance) would
# keep the container alive for exactly as long as the fault it is meant to
# recover from. readiness.py documents this same hazard for its docker ping.
_stop = threading.Event()


class RenewalAlreadyRunning(Exception):
    """A renewal pass is already in progress on this node."""


class InstanceNotFound(Exception):
    """An explicit --instance selector matched nothing on this node."""


class RenewalUnavailable(Exception):
    """The manager has renewal switched off fleet-wide (404, empty body).

    Expected for the whole of a staged rollout, so it is a SKIP and never an
    error. Node-wide by definition: every remaining instance this tick would
    get the same answer, so the tick stops rather than asking N times.
    """


class NodeAuthLost(Exception):
    """The manager rejected this pass's token.

    Node-wide, never the instance's fault: the token rotated between the pass
    reading app.state and the manager receiving the call. Charging it to each
    instance in turn would back off every remaining due one on the node for a
    fault none of them had, and they stay skipped long after the next pass
    picks up the fresh token.
    """


class NodeCapped(Exception):
    """The pass stopped on the manager's per-greffer mint cap.

    Distinct from "no errors": the requested renewals did not happen. An
    operator running the emergency command against a 502ing app must not be
    told it worked.
    """


class Outcome(NamedTuple):
    """What a single instance's pass did, and what it is serving now.

    The status is separate from the serial because most failures here do not
    raise: a manager refusal, a certificate still unserved after a restart, a
    sidecar mid-transition. Returning only the serial made every one of those
    indistinguishable from success, so `renew_certs` exited 0 after renewing
    nothing -- the operator's emergency lever reporting that it had worked.
    """

    status: str
    serial: str | None
    # Why, when the status alone cannot say whether waiting will help. A
    # sidecar inside its settle window clears in ninety seconds; a backoff
    # entry or a missing published port does not. Defaulted so the callers
    # that only care about the status are unchanged.
    reason: str | None = None


class PassResult(NamedTuple):
    """Counts for one pass, plus the outcome of an explicitly selected instance.

    `errors` alone could not answer the operator's question. An instance skipped
    for a real reason -- inside the compose settle window, no published port --
    is not an error, so a targeted `renew_certs --instance X` over a skipped X
    reported a clean pass having renewed nothing. That is the incident sequence:
    restart the app, still 502, run the emergency renewal, be told it worked.
    """

    considered: int
    renewed: int
    skipped: int
    errors: int
    selected: str | None
    # Skipped for something that clears on its own, so the pass that follows
    # should not be a full interval away.
    deferred: int = 0


# Skips that clear on their own within roughly the compose settle window.
# `backoff` and `no_published_port` are deliberately NOT here: one is a
# deliberate wait measured in hours, the other needs an operator.
_TRANSIENT_SKIPS = frozenset({'instance_busy', 'compose_inflight',
                              'sidecar_settling'})

RENEWED = "renewed"
NOT_DUE = "not_due"
SKIPPED = "skipped"
FAILED = "failed"


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
        # Two different 404s, and the manager tells them apart by body: an
        # empty one is CERT_RENEWAL_ENABLED being off fleet-wide, 'not_found'
        # is this instance being gone. Only the second is about this instance.
        #
        # The first is the whole staged rollout -- greffers carry this code for
        # as long as it takes to publish an image and have clients run `greffer
        # update`, all before the manager flag is flipped. Charging that window
        # to each instance in turn made every tick report errors it had no way
        # to act on, and `renew_certs` exit non-zero, on a deployment where
        # nothing is wrong.
        if 'not_found' in res.text:
            logger.debug('cert_renewal_instance_gone instance=%s', greffon_id)
            return None
        logger.debug('cert_renewal_unavailable instance=%s', greffon_id)
        raise RenewalUnavailable
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
        _note_failure(greffon_id, settings.greffer_cert_renewal_interval)
        return None
    if res.status_code in (401, 403):
        logger.warning('cert_renewal_mint_unauthorized instance=%s', greffon_id)
        raise NodeAuthLost
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
        _note_failure(greffon_id, settings.greffer_cert_renewal_interval)
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
        for name, content, mode in (('cert.key', cert['private_key'], 0o600),
                                    ('pem.crt', cert['certificate'], 0o644)):
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(_time.time())
            # Explicit, because tar defaults to 0644 and the start path stages
            # this key through mkstemp (0600) which docker cp preserves. Without
            # it every renewal silently widens an unencrypted TLS private key,
            # in a tree that ships an ops migration to purge strayed copies of
            # this exact file.
            info.mode = mode
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
    # Recorded BEFORE the first request, not after the last. The reasoning
    # below -- a lost debt is terminal, so do not defer the write -- applies
    # to this loop too: three attempts on a 30s timeout plus their backoff
    # sleeps is ~96 seconds against a manager that accepts connections and
    # then stalls. A `greffer update` landing inside THAT window left the
    # sidecar serving a fresh certificate with nothing owed, and no later
    # pass reconciles it, because a just-installed certificate is not due
    # again for thirty days.
    #
    # Safe to write first because every terminal path below clears it: 200,
    # the 409 the manager already acted on, a gone instance. Only the
    # retryable exits leave it standing, which is the point.
    _unconfirmed[greffon_id] = served_serial or ''
    _save_unconfirmed(settings)
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
            _clear_debt(settings, greffon_id)
            return
        if res is not None and res.status_code == 409:
            # serial_mismatch / no_pending_mint / stale. The manager received
            # the report and acted on it; the renewal simply did not land. Not
            # a transport failure, and not retryable -- the pending is gone.
            diag('cert_renewal_report_rejected', instance=greffon_id,
                 body=res.text[:120], level=logging.WARNING)
            _clear_debt(settings, greffon_id)
            return
        if res is not None and res.status_code in (401, 403):
            # A token rotation between this pass reading app.state and the
            # manager receiving the call. The next pass reads a fresh token, so
            # this is retryable -- and it must be retried, because the sidecar
            # is already serving a not-due certificate that no later pass would
            # otherwise report.
            logger.warning('cert_renewal_report_unauthorized instance=%s',
                           greffon_id)
            break
        if (res is not None and res.status_code == 404
                and 'not_found' not in res.text):
            # The report endpoint is gated on CERT_RENEWAL_ENABLED exactly as
            # the mint endpoint is, and answers a disabled manager with the
            # same empty-bodied 404. Terminal is the wrong reading: the flag
            # can go off between the mint and this report -- during the staged
            # rollout, or an operator pausing renewal mid-incident -- and the
            # debt is the ONLY record that this instance is serving something
            # the manager has not been told about. Clearing it strands the
            # pending mint and the stale recorded serial until the certificate
            # comes due again, which for a just-minted one is thirty days of
            # false expiry alarms on a healthy instance.
            #
            # 'not_found' still falls through to the terminal branch below:
            # an instance the manager has forgotten has nothing to reconcile.
            logger.warning(
                'cert_renewal_report_unavailable instance=%s', greffon_id)
            break
        if res is not None and res.status_code < 500:
            logger.warning(
                'cert_renewal_report_failed instance=%s status=%s body=%s',
                greffon_id, res.status_code, res.text[:200],
            )
            _clear_debt(settings, greffon_id)
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
    # Still owed, and already on disk since before the first attempt.
    # Retried at the top of the next pass, before the not-due check that
    # would otherwise skip this instance forever. Recorded even when nothing
    # was served: "we could not tell the manager what this instance is
    # serving" is the debt, and an unobservable sidecar most needs chasing.
    diag('cert_renewal_report_unconfirmed', instance=greffon_id,
         serial=served_serial, level=logging.ERROR)


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
# Single definition, in settings, because the settings validator models
# this same schedule to decide whether a configured window can fit the
# retries. Two copies would drift and the validator would start lying.
_BACKOFF_CAP_SECONDS = CERT_RENEWAL_BACKOFF_CAP_SECONDS


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

    if greffon_id in _unconfirmed:
        # The PROBE is what has to be serialized, and it is the cheap half.
        # A sidecar Compose is recreating serves the old certificate or
        # nothing, for a window that says nothing about what this instance
        # will serve once the operation finishes; reporting that reading
        # retires the manager's pending mint against a value that was never
        # true, and clears the durable debt that is the only record we still
        # owe an answer. Reading it under the lock costs at most one connect
        # plus one handshake, both bounded by _PROBE_TIMEOUT_SECONDS.
        #
        # The REPORT is the expensive half -- three attempts on a 30s HTTP
        # timeout -- and it does not need the lock: it sends a value that was
        # true at probe time, which is the same guarantee the main path gives.
        # Holding the lock across it is the ninety-second hold that the
        # manager's non-blocking control ops turn into terminal 409s, and a
        # migration cutover landing on one is a real failure for a call that
        # is only bookkeeping.
        from app.routers.controller import compose_inflight

        served_now = None
        probed = False
        if not compose_inflight(greffon_id):
            probe_lock = _instance_lock(greffon_id)
            if probe_lock.acquire(blocking=False):
                try:
                    port = _sidecar_host_port(settings, greffon_id)
                    if port is not None:
                        served_now, _ = _served_certificate(
                            settings.greffer_cert_probe_host, port)
                        probed = True
                finally:
                    probe_lock.release()
        if probed:
            # Reports what is served NOW, not the serial the debt was filed
            # under: the debt means "the manager has not been told what this
            # instance is serving", and the handshake is the truthful answer
            # -- including None, which the manager reads as a mismatch and
            # uses to retire its dead pending mint.
            _report(settings, token, greffon_id, served_now)
        else:
            # Deferred, not dropped. The debt survives restarts, so waiting
            # for a quiet tick loses nothing.
            diag('cert_renewal_debt_deferred', instance=greffon_id)

    lock = _instance_lock(greffon_id)
    if not lock.acquire(blocking=False):
        # Non-blocking: a renewal is never urgent enough to queue behind a
        # restore, and holding the tick here would stall every later instance.
        # Not a failure either -- whatever holds the lock (a Start, most
        # likely) mints its own certificate anyway.
        diag('cert_renewal_skipped', instance=greffon_id, reason='instance_busy')
        return Outcome(SKIPPED, None, 'instance_busy')
    try:
        return _renew_locked(settings, token, greffon_id, force)
    finally:
        lock.release()


def _sidecar_settling(greffon_id: str) -> bool:
    """True if the sidecar started so recently that a compose child may still
    be working on it.

    The per-instance lock does NOT cover this on its own. ``compose.start`` and
    ``compose.stop`` are ``subprocess.Popen`` -- they return immediately -- and
    ``_serialize_instance_op`` releases the lock when the HANDLER returns.
    ``_wait_for_compose_running`` only runs on the tunnel branch, so a
    proxy-mode start (and every stop) frees the lock while compose is still
    recreating the container. Writing into a sidecar in that window means our
    certificate is either wiped by the recreate or wins over the one the start
    just recorded, and the manager cannot tell which -- it logs
    ``instance_cert_renewal_orphan`` at CRITICAL and refuses to revoke either.

    Residual: this narrows the window, it does not close it. Closing it needs
    start/stop to hold the lock until compose exits, which is a change to the
    shared control path and not this feature's to make.
    """
    from apps.utils.docker.base import client

    try:
        started = client.containers.get(
            _nginx_container(greffon_id)).attrs['State']['StartedAt']
    except Exception:  # noqa: BLE001 -- unreadable state must not block renewal
        return False
    try:
        # Docker's RFC3339 has nanoseconds; Python takes six digits.
        head, _, tail = started.partition('.')
        cleaned = f'{head}.{tail[:6]}+00:00' if tail else f'{head}+00:00'
        when = dt.datetime.fromisoformat(cleaned.replace('Z', ''))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - when) < dt.timedelta(
        seconds=_COMPOSE_SETTLE_SECONDS)


def _renew_locked(settings: Settings, token: str, greffon_id: str,
                  force: bool) -> Outcome:
    if _in_backoff(greffon_id) and not force:
        diag('cert_renewal_skipped', instance=greffon_id, reason='backoff')
        return Outcome(SKIPPED, None)

    port = _sidecar_host_port(settings, greffon_id)
    if port is None:
        diag('cert_renewal_skipped', instance=greffon_id, reason='no_published_port')
        return Outcome(SKIPPED, None)
    host = settings.greffer_cert_probe_host

    from app.routers.controller import compose_inflight

    if compose_inflight(greffon_id):
        # A start or stop this process launched is still recreating the
        # sidecar. Writing a certificate into it now leaves the manager unable
        # to tell which one is live.
        diag('cert_renewal_skipped', instance=greffon_id, reason='compose_inflight')
        return Outcome(SKIPPED, None, 'compose_inflight')

    if _sidecar_settling(greffon_id):
        # What the in-process signal cannot see: a compose child nobody here
        # spawned (a manual `docker compose up`, a recreate the daemon
        # performed under `restart: unless-stopped`), and the moments right
        # after a greffer restart cleared the counter.
        diag('cert_renewal_skipped', instance=greffon_id, reason='sidecar_settling')
        return Outcome(SKIPPED, None, 'sidecar_settling')

    before_serial, before_expiry = _served_certificate(host, port)
    global _blind_streak
    _blind_streak = _blind_streak + 1 if before_serial is None else 0

    # A confirmation we owed from an earlier pass, before anything else. The
    # certificate is already installed and serving, so the not-due check below
    # would skip this instance forever and the manager would keep alarming on a
    # healthy one while the superseded certificate is never revoked.
    if not force and not _due_for_renewal(
            before_expiry, settings.greffer_cert_renewal_window_days):
        # A healthy certificate settles any old failure. Something else fixed
        # this instance -- a start, a restart, an operator -- and keeping the
        # entry would both inflate `backing_off` forever and make the next
        # unrelated failure resume at the old escalated delay.
        _clear_backoff(greffon_id)
        diag('cert_renewal_skipped', instance=greffon_id, reason='not_due')
        return Outcome(NOT_DUE, before_serial)

    cert = _mint(settings, token, greffon_id)
    if cert is None:
        # 404 (renewal off / instance gone), 429 cooling down, or a permanent
        # 409. Nothing was issued, so there is nothing to install and nothing
        # to report; _mint arms the backoff for the cases that warrant one.
        return Outcome(FAILED, before_serial)
    wanted = cert.get('serial_number')

    # Owed from the INSTANT a certificate exists, not from the first report.
    # The manager is now holding a pending mint, and everything between here
    # and the report -- install, reload, a 20s settle loop, the handshake --
    # is time this process can be stopped or updated in. Without this the
    # next process either re-mints into a pending-mint refusal, or sees the
    # replacement already serving, reads it as not due, and leaves that mint
    # orphaned for thirty days.
    #
    # The value is a placeholder: the debt path re-probes and reports what is
    # served THEN, never the serial it was filed under. This is the last gap
    # of its kind -- before the mint there is nothing owed.
    _unconfirmed[greffon_id] = before_serial or ''
    _save_unconfirmed(settings)

    try:
        _install(greffon_id, cert)
    except Exception:
        # The certificate was ISSUED. Letting this propagate to renew_all's
        # generic handler leaves the manager holding a pending mint nobody ever
        # resolves, against this module's report-on-both-outcomes contract.
        # Report what the sidecar is still serving so the dead mint is retired.
        logger.exception('cert_renewal_install_failed instance=%s', greffon_id)
        _report(settings, token, greffon_id, before_serial)
        _note_failure(greffon_id, settings.greffer_cert_renewal_interval)
        return Outcome(FAILED, before_serial)

    if _reload(greffon_id):
        served, _ = _await_serial(host, port, wanted)
    else:
        # A reload that provably never happened is a different fault from one
        # that was delivered and ignored, and it has nothing to settle. Probe
        # once anyway: the restart escalation below is exactly the fallback for
        # a sidecar that is reachable but did not take the new certificate, and
        # skipping the probe here would deny it that.
        diag('cert_renewal_reload_error', instance=greffon_id,
             level=logging.WARNING)
        served, _ = _served_certificate(host, port)

    if served is None:
        # Cannot see the sidecar at all. NOT a mismatch: restarting a container
        # we are unable to observe turns one broken probe path -- a firewall
        # rule, a greffer started without the host-gateway alias, a sidecar
        # mid-restart -- into a bounce of every healthy instance on the node,
        # every tick, forever. Report the truth and back off instead.
        logger.warning('cert_renewal_unobservable instance=%s port=%s',
                       greffon_id, port)
    elif not _same_serial(served, wanted):
        # The sidecar is reachable and is not serving the new certificate.
        # THIS is the case the whole worker is shaped around: on the stock
        # sidecar image nothing else would ever notice.
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
        diag('cert_renewal_outcome', instance=greffon_id, outcome=RENEWED,
             serial=served)
        _clear_backoff(greffon_id)
        return Outcome(RENEWED, served)
    diag('cert_renewal_outcome', instance=greffon_id, outcome='not_served',
         wanted=wanted, served=served, level=logging.ERROR)
    _note_failure(greffon_id, settings.greffer_cert_renewal_interval)
    return Outcome(FAILED, served)


def renew_all(settings: Settings, token: str, only: str | None = None,
              force: bool = False) -> PassResult:
    """One tick over every instance on this greffer. Returns the error count.

    ``token`` is the one THIS process registered with, passed in rather than
    resolved here. ``resolve_token`` mints a fresh random token every call when
    the token file cannot be read or written -- a supported degraded mode in
    which the app keeps a single ephemeral token in ``app.state``. Resolving
    per pass would hand the manager a different claimant each time, 403 every
    renewal, and expire every certificate on the node.

    Per-instance failures are contained: one unreachable sidecar must not stop
    the others from renewing, because they expire on their own schedules and a
    single bad instance would otherwise take the whole fleet down with it.
    """
    from app.workers.status_collect import collect_status_map

    if not _pass_lock.acquire(blocking=False):
        raise RenewalAlreadyRunning
    try:
        return _renew_all_locked(settings, collect_status_map, only, force,
                                 token)
    finally:
        _pass_lock.release()


def _renew_all_locked(settings, collect_status_map, only, force,
                      token) -> PassResult:
    _load_unconfirmed(settings)
    considered = errors = renewed = skipped = 0
    selected = None
    capped = unauthorized = False
    deferred = 0
    global _blind_streak
    _blind_streak = 0
    if _stop.is_set():
        return PassResult(0, 0, 0, 0, None)
    statuses = collect_status_map(settings)
    if _stop.is_set():
        # The sweep is the single longest blocking call in a pass -- one docker
        # round trip per instance, each able to burn the SDK's 60s timeout
        # against a hung daemon. Checking only between instances would still
        # let a shutdown wait out the whole sweep first.
        diag('cert_renewal_pass_interrupted', considered=0, level=logging.WARNING)
        return PassResult(0, 0, 0, 0, None)
    if only is not None and only not in statuses:
        # A typo'd or stale id would otherwise skip every instance and report a
        # clean pass, so the operator's emergency command would exit 0 having
        # touched nothing.
        raise InstanceNotFound(only)
    for greffon_id, status in statuses.items():
        if only is not None and greffon_id != only:
            continue
        if status != 'running':
            # A stopped instance has no sidecar to reload, and its certificate
            # is re-minted at start anyway.
            continue
        if _stop.is_set():
            # Shutting down. Stop between instances rather than mid-install, so
            # the join at exit is bounded by ONE instance instead of the fleet.
            diag('cert_renewal_pass_interrupted', considered=considered,
                 level=logging.WARNING)
            break
        considered += 1
        try:
            # A failure that does not raise is still a failure. Counting only
            # exceptions had `renew_certs` exit 0 after a manager refusal or a
            # certificate still unserved past the restart.
            outcome = renew_one(settings, token, greffon_id, force=force)
            if greffon_id == only:
                selected = outcome.status
            if outcome.status == FAILED:
                errors += 1
            elif outcome.status == RENEWED:
                renewed += 1
            elif outcome.status == SKIPPED:
                skipped += 1
                if outcome.reason in _TRANSIENT_SKIPS:
                    deferred += 1
        except RenewalUnavailable:
            # Node-wide, so stop -- but unlike the auth and cap breaks this is
            # not a warning: the deployment simply has not opted in yet.
            # No flag to carry: unlike the unauthorized and capped breaks
            # below, this one must NOT raise. Exiting non-zero because the
            # manager has not opted in yet is the exact false alarm this
            # branch exists to remove.
            diag('cert_renewal_tick_unavailable', considered=considered)
            skipped += 1
            if greffon_id == only:
                selected = SKIPPED
            break
        except NodeAuthLost:
            # Every remaining instance would present the same rejected token.
            diag('cert_renewal_tick_unauthorized', considered=considered,
                 level=logging.WARNING)
            unauthorized = True
            break
        except _NodeRateLimited:
            # The node hit the manager's per-greffer cap. Every remaining
            # instance would collect the same refusal, so stopping here is not
            # giving up: it leaves the rest un-penalised and due again next
            # tick, which converges far faster than backing each of them off
            # exponentially for a limit none of them caused.
            diag('cert_renewal_tick_capped', considered=considered,
                 level=logging.WARNING)
            capped = True
            break
        except Exception:  # one instance must not end the tick
            errors += 1
            if greffon_id == only:
                selected = FAILED
            # Armed HERE too, not only on a clean mismatch. A sidecar that has
            # been deleted, or a manager that is unreachable, raises out of
            # _mint / _install rather than returning -- and those are the
            # failures most likely to repeat. Without this they retry at the
            # full tick rate forever, each one burning a fresh 30-day mint out
            # of the budget the backoff exists to protect.
            _note_failure(greffon_id, settings.greffer_cert_renewal_interval)
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
    # Same reasoning for the owed-confirmation ledger: its entries are only
    # ever cleared inside _renew_locked, which a departed instance never
    # reaches again.
    for stale in set(_unconfirmed) - set(statuses):
        _unconfirmed.pop(stale, None)
    _save_unconfirmed(settings)
    # renewed/skipped broken out, not just considered/errors. `considered`
    # counts instances LOOKED AT, so a node where all 200 are skipped for
    # no_published_port logged considered=200 errors=0 -- indistinguishable
    # from a node that renewed everything.
    diag('cert_renewal_tick', considered=considered, renewed=renewed,
         skipped=skipped, errors=errors, backing_off=len(_renewal_backoff),
         owed=len(_unconfirmed))
    if unauthorized:
        # NOT a clean pass. Returning normally had the operator's emergency
        # command print "renewal pass complete, errors=0" and exit 0 after the
        # manager rejected this node's token -- the same failure the NodeCapped
        # mapping below exists to prevent.
        raise NodeAuthLost
    if capped:
        raise NodeCapped
    return PassResult(considered, renewed, skipped, errors, selected, deferred)


async def cert_renewal_worker(app: FastAPI) -> None:
    """Periodically renew per-instance upstream certificates.

    Sleep-first like the other workers, but only briefly. The premise that
    made a full-interval first sleep look safe -- "certificates are minted at
    start, so nothing is due after boot" -- is about an INSTANCE start. This
    worker boots with the greffer process, and a greffer restart leaves every
    running sidecar serving exactly the certificate it had before, including
    one that expired while the process was down. Since `greffer update` is how
    a node receives this feature, a six-hour first sleep would let the 502s it
    exists to fix outlive the fix itself by six hours.

    So the first pass waits a jittered few minutes instead: long enough that a
    fleet restarting together does not mint in lockstep against one manager,
    short enough that an already-expired certificate is found on the day it is
    deployed rather than the next morning.
    """
    settings: Settings = app.state.settings
    if not settings.greffer_cert_renewal_enabled:
        # A per-node off switch. The manager's CERT_RENEWAL_ENABLED is
        # fleet-wide, so without this an operator seeing one node misbehave can
        # only stop renewal everywhere.
        logger.info('cert renewal worker disabled by CERT_RENEWAL_ENABLED')
        return
    # The delay before the NEXT pass, carried across iterations. A bare
    # `sleep(_CAPPED_RETRY_SECONDS)` inside the handler does not shorten
    # anything: control returns to the top and sleeps the full interval as
    # well, so the advertised one-hour retry actually happened seven hours
    # later -- worse than no retry, because the log claimed otherwise.
    # Clamped so a short configured interval is never lengthened by the
    # jitter: the validator admits intervals as low as 60s.
    delay = min(random.uniform(0, _STARTUP_JITTER_SECONDS),
                settings.greffer_cert_renewal_interval)
    diag('cert_renewal_first_pass_scheduled', delay_seconds=round(delay))
    try:
        while True:
            await asyncio.sleep(delay)
            delay = settings.greffer_cert_renewal_interval
            # Registration first, exactly as heartbeat_worker does. A pass that
            # runs during initial acceptance or a re-registration gets 403 on
            # every request, and _mint reads 403 as this instance's own failure
            # -- so one badly timed tick puts every due instance on the node
            # into a 6-to-24-hour backoff for a fault none of them had.
            await app.state.registered.wait()
            try:
                # Re-read every tick, never snapshot at startup. register.py
                # rewrites app.state.greffer_token on rotation (a degraded-boot
                # ephemeral token later superseded by an on-disk one, a restore,
                # an operator rotation), and monitor.py / heartbeat.py both
                # re-read for exactly this reason. A stale token 403s every mint
                # and report, and _mint reads 403 as a generic refusal, so each
                # instance walks the backoff to the cap and the whole node's
                # certificates expire silently.
                # Cleared HERE, before the offload. Clearing inside the pass
                # erased a cancellation that arrived between scheduling and the
                # thread starting, and the pass then ran on to completion with
                # the loop already gone.
                _stop.clear()
                result = await anyio.to_thread.run_sync(
                    renew_all, settings, app.state.greffer_token,
                    abandon_on_cancel=True
                )
                if result is not None and result.deferred:
                    # Something was skipped for a condition that clears on its
                    # own. Waiting a full interval for it is how an expired
                    # certificate survives the pass that existed to catch it.
                    diag('cert_renewal_tick_deferred_retry',
                         deferred=result.deferred,
                         delay_seconds=_DEFERRED_RETRY_SECONDS)
                    delay = min(_DEFERRED_RETRY_SECONDS,
                                settings.greffer_cert_renewal_interval)
            except RenewalAlreadyRunning:
                # An operator's renew_certs holds the pass. Not a crash, and
                # logging it as one buried a real traceback in false alarms.
                #
                # But this tick renewed NOTHING, and the pass that displaced
                # it is usually a targeted --instance one that renews exactly
                # its own instance. Every other due instance on the node just
                # lost its turn, so come back like any other deferred pass
                # rather than waiting the full interval for a collision that
                # lasted a minute.
                diag('cert_renewal_tick_skipped', reason='pass_in_progress',
                     delay_seconds=_DEFERRED_RETRY_SECONDS)
                delay = min(_DEFERRED_RETRY_SECONDS,
                            settings.greffer_cert_renewal_interval)
            except NodeAuthLost:
                # The manager rejected this node's token, so the pass aborted
                # having renewed nothing. The heartbeat and register workers
                # install a replacement within seconds, and the wait() at the
                # top of this loop already gates the retry on re-registration
                # -- so the only thing a full interval buys is six hours of
                # expiry on every instance the aborted pass skipped.
                diag('cert_renewal_tick_unauthorized_retry',
                     delay_seconds=_DEFERRED_RETRY_SECONDS,
                     level=logging.WARNING)
                delay = min(_DEFERRED_RETRY_SECONDS,
                            settings.greffer_cert_renewal_interval)
            except NodeCapped:
                # Expected on a node with more due instances than the manager's
                # hourly budget. Retry sooner than the full interval: at 30/h
                # against a 6h tick the node would otherwise use a sixth of its
                # own budget and take ~40h to clear 200 expired certificates.
                diag('cert_renewal_tick_capped_retry',
                     delay_seconds=_CAPPED_RETRY_SECONDS)
                # Clamped: the validator admits intervals as short as 60s,
                # and an unconditional hour would make a rate-limited node wait
                # LONGER than it otherwise would -- the inverse of the point.
                delay = min(_CAPPED_RETRY_SECONDS,
                            settings.greffer_cert_renewal_interval)
            except Exception:  # the loop outlives any one tick
                logger.exception('cert_renewal_tick_failed')
    except asyncio.CancelledError:
        _stop.set()
        logger.info('cert renewal worker stopped')
        raise
