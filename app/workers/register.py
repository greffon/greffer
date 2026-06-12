"""Register worker — POST to manager, poll for cert, install, fetch initial CRL.

Ports ``apps/utils/greffon/base_server.py:register()`` to asyncio. The outer
loops are rewritten as async so they cancel cleanly on lifespan shutdown;
inner blocking calls (``requests``, docker SDK) run in a threadpool via
``anyio.to_thread.run_sync``.

Token is read from ``app.state.greffer_token`` (set by ``create_app`` in
feature #1), not from the Django module global in ``apps/utils/auth.py``.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import anyio
import requests
from fastapi import FastAPI

from app.settings import Settings
from app.token import load_persisted_token, resolve_token

logger = logging.getLogger("greffer")


def _inflight_token(app: FastAPI, settings: Settings) -> str:
    """Freshest token to use for the current (re-)registration attempt.

    An operator-pinned ``GREFFER_TOKEN`` wins; otherwise a PERSISTED on-disk
    token (re-read each retry so a rotation that lands mid-flight is picked up);
    otherwise the stable token already in ``app.state``. Crucially this never
    re-mints the ephemeral in-memory fallback that ``load_or_create_token`` uses
    when ``GREFFON_PATH`` is unwritable: minting a new token per cert poll would
    diverge from the token the register POST staged and 403 forever.
    """
    if settings.greffer_token:
        return settings.greffer_token
    persisted = load_persisted_token(settings.greffon_path / ".greffer-token")
    return persisted or app.state.greffer_token


_REGISTER_RETRY_SECONDS = 3.0
# Cap for the exponential backoff on a persistently-refused register. A 400
# (mode_mismatch / invalid fields) never self-heals, so a fixed 3s retry would
# emit ~28k ERROR lines/day forever; backing off to once a minute keeps the
# signal loud without drowning the log.
_REGISTER_RETRY_MAX_SECONDS = 60.0
_CERT_POLL_SECONDS = 5.0
# Cert-poll log throttle: the worker polls every 5s, but the two steady-state
# non-200s (401 awaiting acceptance, 403 invalid token) are conditions that can
# persist for minutes/hours. Log on every status *transition*, then only once
# per this many identical polls as a heartbeat (12 * 5s ≈ once a minute) so a
# normal wait-for-admin doesn't produce 720 INFO lines/hour.
_CERT_LOG_HEARTBEAT_EVERY = 12
_HTTP_TIMEOUT_SECONDS = 10.0


class RegisterRejected(Exception):
    """The manager answered the register POST with a non-2xx status.

    Carries the HTTP status and (truncated) response body so the
    register loop can log a *loud, actionable* line instead of silently
    falling through to the cert-poll phase. The silent fall-through was a
    real outage mode: a 409 ``greffer_id_claimed`` (or 400 ``mode_mismatch``,
    429 ``rate_limited``) left the greffer polling the cert endpoint
    forever, every poll 403ing because the manager never staged this
    greffer's token — with nothing in the greffer's own logs to say why.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"register rejected: HTTP {status_code}: {body}")


async def register_worker(app: FastAPI) -> None:
    """Initial registration: POST until accepted, poll for cert, install.
    Returns once registration completes (which sets ``app.state.registered``,
    un-gating the heartbeat). Retries the whole flow on an unexpected failure
    (e.g. a transient docker error during cert install) so such a failure does
    NOT leave ``registered`` unset and the heartbeat blocked forever. Later
    re-runs (after a heartbeat 403) are handled by ``reregister_worker``."""
    settings: Settings = app.state.settings
    delay = _REGISTER_RETRY_SECONDS
    while True:
        try:
            # Resolve the address inside the retry loop: a DNS failure
            # (_resolve_hostname -> socket.gaierror) must also retry rather than
            # kill the task and leave the heartbeat parked.
            address = await _resolve_address(settings)
            await _run_registration(app, settings, address)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "initial registration failed; retrying in %ss", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _REGISTER_RETRY_MAX_SECONDS)


async def reregister_worker(app: FastAPI) -> None:
    """Supervisor that re-runs registration on demand (greffer-observability
    epic). The heartbeat sets ``app.state.reregister_requested`` on a 403 (the
    manager rejected our accepted token); we re-read the on-disk token, in case
    the operator rotated it, and re-register. Idle the rest of the time."""
    settings: Settings = app.state.settings
    event: asyncio.Event = app.state.reregister_requested
    try:
        while True:
            await event.wait()
            event.clear()
            logger.warning("re-register requested; re-running registration")
            # Retry until registration succeeds (which sets app.state.registered
            # and un-gates the heartbeat). A single failed attempt must NOT
            # return to idle: registered is cleared and reregister_requested is
            # cleared, so nothing would ever re-trigger us and the heartbeat
            # would stay parked forever. Mirror register_worker's retry loop.
            delay = _REGISTER_RETRY_SECONDS
            while True:
                try:
                    app.state.greffer_token = resolve_token(settings)
                    address = await _resolve_address(settings)
                    await _run_registration(app, settings, address)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "re-registration attempt failed; retrying in %ss", delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _REGISTER_RETRY_MAX_SECONDS)
    except asyncio.CancelledError:
        logger.info("reregister supervisor cancelled")
        raise


async def _resolve_address(settings: Settings) -> str:
    return settings.greffer_address or await anyio.to_thread.run_sync(
        _resolve_hostname, abandon_on_cancel=True
    )


async def _run_registration(
    app: FastAPI, settings: Settings, address: str
) -> None:
    """Register with the manager and block until the cert is installed.

    Re-resolves the token before every register POST and every cert poll (and
    mirrors it into ``app.state``) so a rotation landing mid-flight is picked up
    instead of looping forever on a stale token. All ``anyio.to_thread.run_sync`` calls use
    ``abandon_on_cancel=True`` so lifespan shutdown doesn't block on a hung HTTP
    call; every HTTP call also carries a ``timeout``.
    """
    # Phase 1: POST until the manager accepts the registration (2xx). Retries
    # back off exponentially (capped) so a permanently-refused register stays
    # loud without flooding the log.
    delay = _REGISTER_RETRY_SECONDS
    while True:
        # Re-resolve the token each attempt so a rotation landing mid-flight
        # (operator re-pins GREFFER_TOKEN, or the manager stages a new one after
        # the 403 that triggered this re-register) is picked up, instead of
        # looping forever on a stale token captured at entry. Keep app.state in
        # sync so the heartbeat worker beats with the same token post-register.
        token = app.state.greffer_token = _inflight_token(app, settings)
        try:
            await anyio.to_thread.run_sync(
                _post_register,
                settings,
                address,
                token,
                abandon_on_cancel=True,
            )
            break
        except (requests.ConnectionError, requests.Timeout):
            # Timeout covers both ConnectTimeout and ReadTimeout — the
            # latter isn't a subclass of ConnectionError, so we need it
            # explicitly to retry the POST on a slow/hung manager.
            logger.info(
                "manager not reachable at %s, retrying in %ss",
                settings.greffon_base_server,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _REGISTER_RETRY_MAX_SECONDS)
        except RegisterRejected as exc:
            # The manager is reachable but refused this registration. DON'T
            # fall through to cert polling — without a staged token the cert
            # endpoint will 403 every poll forever. Log loudly (the body
            # names the reason: greffer_id_claimed / mode_mismatch / etc.)
            # and keep retrying with backoff: a 409 clears once the stale
            # claim is reset (e.g. operator pins GREFFER_TOKEN or resets the
            # claim), a 429/503 clears on its own. A 400 won't self-heal, but
            # a backed-off loud error beats a silent dead-end either way.
            logger.error(
                "register refused by manager (HTTP %s): %s — retrying in %ss. "
                "Cert issuance is blocked until this register succeeds.",
                exc.status_code,
                exc.body,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _REGISTER_RETRY_MAX_SECONDS)

    # Phase 2: poll for cert until 200. Catch transient network errors
    # so a blip after the initial POST doesn't terminate the worker and
    # leave the greffer stuck unregistered until process restart. Cert-poll
    # status logging is throttled (transition + heartbeat) because 401/403
    # can persist; see _CERT_LOG_HEARTBEAT_EVERY.
    last_status: int | None = None
    same_status_polls = 0
    while True:
        # Re-resolve per poll for the same reason as Phase 1: a 403-looping cert
        # poll must observe a token rotated mid-flight rather than 403 forever.
        token = app.state.greffer_token = _inflight_token(app, settings)
        try:
            data, cert_status = await anyio.to_thread.run_sync(
                _fetch_cert, settings, token, abandon_on_cancel=True
            )
        except (requests.ConnectionError, requests.Timeout):
            logger.info(
                "manager cert endpoint unreachable, retrying in %ss",
                _CERT_POLL_SECONDS,
            )
            await asyncio.sleep(_CERT_POLL_SECONDS)
            continue
        if data is None:
            # Throttle: log on every status transition, then once per
            # heartbeat window so a long awaiting-acceptance (401) or a stuck
            # invalid-token (403) is visible without spamming every 5s.
            if cert_status != last_status:
                _log_cert_poll_status(cert_status, first=True)
                last_status = cert_status
                same_status_polls = 0
            else:
                same_status_polls += 1
                if same_status_polls % _CERT_LOG_HEARTBEAT_EVERY == 0:
                    _log_cert_poll_status(cert_status, first=False)
            await asyncio.sleep(_CERT_POLL_SECONDS)
            continue
        # data is not None -> 200, cert issued. Install and finish.
        await anyio.to_thread.run_sync(
            _install_cert, settings, data, abandon_on_cancel=True
        )
        # v3 push: the manager embeds the initial rathole client.toml
        # in the cert response on accept (tunnel mode only). Write it
        # before the worker exits so rathole-client can come up
        # immediately. Absent for proxy-mode greffers and for the
        # transitional v2-manager-+-v3-greffer combo (in which case
        # the v2 polling sidecar handles updates instead). Failure
        # is logged but does NOT abort the register flow — without
        # this file the greffer is still functional in proxy mode
        # and the next start/stop push will retry.
        await anyio.to_thread.run_sync(
            _maybe_install_initial_tunnel_config,
            settings,
            data,
            abandon_on_cancel=True,
        )
        await anyio.to_thread.run_sync(
            _fetch_and_store_crl, settings, abandon_on_cancel=True
        )
        # Let the heartbeat worker start beating now that we are accepted and
        # hold a cert (greffer-observability epic).
        app.state.registered.set()
        logger.info("register complete")
        return


# ---------------------------------------------------------------------------
# Sync helpers — kept at module scope so they're unit-testable with plain
# pytest (no event loop needed).
# ---------------------------------------------------------------------------


def _resolve_hostname() -> str:
    hostname = socket.gethostname()
    return socket.gethostbyname(hostname)


def _safe_body(res: requests.Response, limit: int = 500) -> str:
    """Best-effort, length-capped response body for log lines. Never raises
    (a body that isn't decodable text must not mask the original HTTP error)."""
    try:
        text = res.text
    except Exception:
        return "<unreadable body>"
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _post_register(settings: Settings, address: str, token: str) -> None:
    payload = {
        "address": address,
        # The legacy code posts ``port`` as a str (reads from env as-is).
        # Pydantic-settings types it as int; coerce at the wire boundary.
        "port": str(settings.greffer_port),
        "token": token,
        "protocol": settings.greffer_protocol,
        # Greffer software version (always sent). The manager stamps
        # Greffer.version from this on every (re-)register and uses it for the
        # per-greffon min_greffer_version compatibility gate. An older greffer
        # without this field leaves Greffer.version null -> treated as below any
        # floor (fail-safe deny).
        "version": settings.greffer_version,
    }
    # Include greffer_mode in the register payload only when the operator
    # has set it explicitly. The manager's register endpoint accepts an
    # optional ``mode`` field and validates against ``Greffer.mode`` —
    # match → 200, mismatch → 400. With ``greffer_mode`` unset, manager
    # defaults the validation against MODE_PROXY (preserving the
    # pre-tunnel-feature behaviour for any greffer that hasn't been
    # flipped to tunnel mode at the manager). Operators who flip mode
    # via ``PATCH /api/greffer/{id}/mode/`` must also set
    # ``GREFFER_MODE=tunnel`` here and restart greffer so its register
    # payload aligns with the new stored mode.
    if settings.greffer_mode:
        payload["mode"] = settings.greffer_mode
    res = requests.post(
        f"{settings.greffon_base_server}/api/greffer/register/{settings.greffer_id}/",
        json=payload,
        verify=settings.greffer_ssl_verify,
        # Timeout is a safety net so the thread can't hang forever on a
        # stalled manager. Paired with abandon_on_cancel=True on the caller
        # side so shutdown is snappy regardless.
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    # A reachable-but-refusing manager (4xx/5xx) must NOT look like success.
    # raise_for_status() drops the body, and the body is the whole point
    # here (it names the rejection reason), so check explicitly and carry
    # a truncated body up to the loop. Truncate so a misbehaving manager
    # can't flood the log with one line.
    if not 200 <= res.status_code < 300:
        raise RegisterRejected(res.status_code, _safe_body(res))


def _fetch_cert(settings: Settings, token: str) -> tuple[dict[str, Any] | None, int]:
    # ``X-Greffer-Token`` authenticates the cert poll: the response carries
    # the greffer's private key (and, in tunnel mode, the rathole client
    # config embedding the tunnel token), so the manager must be able to
    # tell the registered greffer apart from anyone who knows its UUID.
    # A custom header (not ``Authorization: Token ...``) because the manager
    # runs DRF TokenAuthentication globally: presenting a non-DRF token
    # there would 401 the request before the view runs. Managers that
    # don't enforce yet simply ignore the header.
    #
    # Returns ``(data, status_code)``: data is the parsed body on 200, else
    # None. The status is returned (not logged here) so the caller's poll
    # loop — which holds the cross-poll state — can throttle logging.
    res = requests.get(
        f"{settings.greffon_base_server}/api/greffer/certificate/{settings.greffer_id}/",
        headers={"X-Greffer-Token": token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    if res.status_code == 200:
        return res.json(), res.status_code
    return None, res.status_code


def _log_cert_poll_status(status_code: int, *, first: bool) -> None:
    """Log a non-200 cert-poll status. ``first`` is True on a status
    transition (always logged) and False on a heartbeat tick (periodic
    reminder). The two expected non-200s mean very different things:
      401 -> registered but not yet accepted by an admin (normal wait).
             Logged at INFO on entry, then heartbeat INFO ~once a minute.
      403 -> invalid_greffer_token: the manager does not recognise this
             greffer's token. A stuck state, not a wait — stale/rotated
             token or a register that never succeeded (see RegisterRejected).
             WARNING on entry and on each heartbeat so it stays visible.
    """
    if status_code == 401:
        if first:
            logger.info("cert not issued yet — awaiting admin acceptance (HTTP 401)")
        else:
            logger.info("still awaiting admin acceptance (HTTP 401)")
    elif status_code == 403:
        logger.warning(
            "cert poll rejected: invalid_greffer_token (HTTP 403). The manager "
            "does not recognise this greffer's token — registration has not "
            "succeeded (stale token, or a refused register POST). This will not "
            "self-resolve by polling.",
        )
    else:
        logger.warning("unexpected cert poll status: HTTP %s", status_code)


def _install_cert(settings: Settings, data: dict[str, Any]) -> None:
    # Imported lazily so unit tests can mock the helper before the docker
    # SDK initializes (``apps/utils/docker/base`` creates a client at import).
    from apps.utils.docker.base import copy_file_into_container

    nginx = settings.docker_nginx_name
    copy_file_into_container(nginx, "/root", "pem.crt", data["certificate"])
    copy_file_into_container(nginx, "/root", "cert.key", data["private_key"])
    if "issuing_ca" in data:
        copy_file_into_container(nginx, "/root", "ca.pem", data["issuing_ca"])


def _maybe_install_initial_tunnel_config(
    settings: Settings, data: dict[str, Any]
) -> None:
    """Write the manager-pushed initial ``client.toml`` if present.

    The manager only includes ``tunnel_client_toml`` in the cert
    response when the greffer is in tunnel mode AND its status reached
    GREFFER_REGISTERED (i.e. admin accepted). Field absence is the
    common case (proxy-mode greffer, or v2 manager that doesn't
    push). Treat both branches as success — the greffer is still
    functional regardless.

    Failure here is non-fatal: log and continue. The greffer's nginx,
    docker-compose lifecycle, and proxy-mode operations don't depend
    on this file. The next start/stop push will retry; the operator
    will notice via the manager's surfaced ``config_write_status`` if
    the issue persists.
    """
    # Lazy import so unit tests can mock the helper without instantiating
    # the FastAPI app.
    from app.tunnel_config import maybe_write_client_toml

    content = data.get("tunnel_client_toml")
    target = settings.greffer_tunnel_client_config_path
    # Catch broadly: ``data`` is the parsed JSON body of the cert response;
    # a misbehaving / compromised manager could return ``tunnel_client_toml``
    # as something other than a string (dict, list, int) and the underlying
    # f.write() would raise TypeError — escaping past a narrow OSError
    # except, aborting the register-worker, and breaking a flow whose
    # docstring promises non-fatal behaviour. The non-fatal contract is
    # the entire reason this branch exists; honouring it requires
    # catching everything. (Codex P2 on greffer#25.)
    try:
        wrote = maybe_write_client_toml(content, target)
    except Exception as exc:
        logger.warning(
            "initial_tunnel_config_write_failed (non-fatal): %s", exc
        )
        return
    if wrote:
        logger.info("initial_tunnel_config_installed path=%s", target)


def _fetch_and_store_crl(settings: Settings) -> None:
    """Fetch CRL from manager, copy into nginx container. Shared with
    ``crl_sync_worker`` (see ``app/workers/crl.py``)."""
    from apps.utils.docker.base import copy_file_into_container

    try:
        res = requests.get(
            f"{settings.greffon_base_server}/api/greffer/ca/crl/",
            verify=settings.greffer_ssl_verify,
            timeout=10,
        )
        if res.status_code == 200:
            copy_file_into_container(
                settings.docker_nginx_name, "/root", "revoked.crl", res.text
            )
            logger.info("CRL updated successfully")
        else:
            logger.warning("Failed to fetch CRL: HTTP %s", res.status_code)
    except Exception as e:
        # Preserve legacy behavior: log and continue. The caller's async
        # loop handles retry timing.
        logger.warning("Failed to fetch CRL: %s", e)
