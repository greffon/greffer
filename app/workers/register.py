"""Register worker — POST to manager, poll for cert, install, fetch initial CRL.

Ports ``apps/utils/greffon/base_server.py:register()`` to asyncio. The outer
loops are rewritten as async so they cancel cleanly on lifespan shutdown;
inner blocking calls (``requests``, docker SDK) run in a threadpool via
``anyio.to_thread.run_sync``.

Token is read from ``app.state.greffer_token`` (set by ``create_app`` in
feature #1), not from the Django module global in ``apps/utils/auth.py``.

This module also owns the outbound mTLS helpers (cert paths, atomic
write, ``_client_auth``, secure-bootstrap check) that the other workers
import — the greffer's identity on the wire lives here end to end.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path
from typing import Any

import anyio
import requests
from fastapi import FastAPI

from app.settings import Settings

logger = logging.getLogger("greffer")


_REGISTER_RETRY_SECONDS = 3.0
_CERT_POLL_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 10.0

_CERT_FILE = "pem.crt"
_KEY_FILE = "cert.key"
_CA_FILE = "ca.pem"


# ---------------------------------------------------------------------------
# mTLS helpers — shared with monitor_worker and crl_sync_worker.
# ---------------------------------------------------------------------------


def _cert_paths(settings: Settings) -> tuple[Path, Path, Path]:
    """(cert, key, ca) paths under ``settings.greffer_cert_dir``."""
    base = settings.greffer_cert_dir
    return base / _CERT_FILE, base / _KEY_FILE, base / _CA_FILE


def _write_local_cert(
    settings: Settings, file_name: str, content: str, mode: int = 0o644
) -> None:
    """Write atomically: tmp file with explicit mode, then ``os.rename``.

    Prevents another worker from reading a truncated or half-written PEM
    in the window between ``O_TRUNC`` and ``write``. Rename is atomic on
    POSIX so ``os.path.exists`` on the destination is a sufficient
    precondition that its bytes are durable."""
    settings.greffer_cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = settings.greffer_cert_dir / file_name
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.rename(str(tmp), str(path))


def _client_auth(settings: Settings) -> dict[str, Any]:
    """Return ``requests`` kwargs.

    Present the greffer's client cert whenever cert+key are on disk
    (post-registration). CA presence is an independent verify-override
    since ``issuing_ca`` is optional in the cert response; fall back to
    system-CA verification when the manager didn't ship its CA.

    Invariant: register() writes key before cert, so
    ``cert_path.exists()`` implies ``key_path.exists()``. No half-written
    pair can ever be loaded by this function."""
    cert_path, key_path, ca_path = _cert_paths(settings)
    kwargs: dict[str, Any] = {"verify": True}
    if ca_path.exists():
        kwargs["verify"] = str(ca_path)
    if cert_path.exists() and key_path.exists():
        kwargs["cert"] = (str(cert_path), str(key_path))
    return kwargs


def _check_secure_bootstrap(settings: Settings) -> None:
    """Refuse to start registration over a non-https channel.

    The bootstrap POST carries the token, and the cert-poll GET returns
    the greffer's private key in the response body. An operator copying
    dev env.env to prod must make a conscious choice via
    ``GREFFER_ALLOW_INSECURE_BOOTSTRAP=1``."""
    if settings.greffon_base_server.startswith("https://"):
        return
    if settings.greffer_allow_insecure_bootstrap:
        logger.warning(
            "GREFFON_BASE_SERVER=%r is insecure — token and private key will "
            "be sent in cleartext. This must be https:// in production.",
            settings.greffon_base_server,
        )
        return
    raise RuntimeError(
        f"GREFFON_BASE_SERVER={settings.greffon_base_server!r} is not https. "
        "The bootstrap register/cert-poll calls carry the greffer token and "
        "receive the greffer private key in the response body. Set "
        "GREFFER_ALLOW_INSECURE_BOOTSTRAP=1 to permit (dev only)."
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


async def register_worker(app: FastAPI) -> None:
    """Register with the manager and block until cert is installed.

    All ``anyio.to_thread.run_sync`` calls use ``abandon_on_cancel=True`` so
    that lifespan shutdown doesn't block waiting for a hung HTTP call — the
    async task returns immediately on cancel and the thread finishes (or
    gets killed at process exit) in the background. Every HTTP call also
    carries a ``timeout`` so the thread can't hang forever in the first
    place.

    On successful cert install this sets ``app.state.registered`` (an
    ``asyncio.Event``) so ``monitor_worker`` can gate its loop on it —
    status callbacks before registration have no client cert to present
    and would be rejected by the manager's mTLS location gate.
    """
    settings: Settings = app.state.settings
    token: str = app.state.greffer_token
    _check_secure_bootstrap(settings)

    address = settings.greffer_address or await anyio.to_thread.run_sync(
        _resolve_hostname, abandon_on_cancel=True
    )

    # Phase 1: POST until the manager is reachable at all.
    while True:
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
                _REGISTER_RETRY_SECONDS,
            )
            await asyncio.sleep(_REGISTER_RETRY_SECONDS)

    # Phase 2: poll for cert until 200. Catch transient network errors
    # so a blip after the initial POST doesn't terminate the worker and
    # leave the greffer stuck unregistered until process restart.
    while True:
        try:
            data = await anyio.to_thread.run_sync(
                _fetch_cert, settings, abandon_on_cancel=True
            )
        except (requests.ConnectionError, requests.Timeout):
            logger.info(
                "manager cert endpoint unreachable, retrying in %ss",
                _CERT_POLL_SECONDS,
            )
            await asyncio.sleep(_CERT_POLL_SECONDS)
            continue
        if data is not None:
            try:
                _validate_cert_response(data)
            except ValueError as e:
                logger.error("malformed cert response: %s", e)
                await asyncio.sleep(_CERT_POLL_SECONDS)
                continue
            await anyio.to_thread.run_sync(
                _install_cert, settings, data, abandon_on_cancel=True
            )
            await anyio.to_thread.run_sync(
                _fetch_and_store_crl, settings, abandon_on_cancel=True
            )
            _mark_registered(app)
            logger.info("register complete")
            return
        await asyncio.sleep(_CERT_POLL_SECONDS)


def _mark_registered(app: FastAPI) -> None:
    event = getattr(app.state, "registered", None)
    if event is not None:
        event.set()


# ---------------------------------------------------------------------------
# Sync helpers — kept at module scope so they're unit-testable with plain
# pytest (no event loop needed).
# ---------------------------------------------------------------------------


def _resolve_hostname() -> str:
    hostname = socket.gethostname()
    return socket.gethostbyname(hostname)


def _post_register(settings: Settings, address: str, token: str) -> None:
    requests.post(
        f"{settings.greffon_base_server}/api/greffer/register/{settings.greffer_id}/",
        json={
            "address": address,
            # The legacy code posts ``port`` as a str (reads from env as-is).
            # Pydantic-settings types it as int; coerce at the wire boundary.
            "port": str(settings.greffer_port),
            "token": token,
            "protocol": settings.greffer_protocol,
        },
        timeout=_HTTP_TIMEOUT_SECONDS,
        **_client_auth(settings),
    )


def _fetch_cert(settings: Settings) -> dict[str, Any] | None:
    res = requests.get(
        f"{settings.greffon_base_server}/api/greffer/certificate/{settings.greffer_id}/",
        timeout=_HTTP_TIMEOUT_SECONDS,
        **_client_auth(settings),
    )
    return res.json() if res.status_code == 200 else None


def _validate_cert_response(data: dict[str, Any]) -> None:
    """A 200 with missing required fields is a manager bug (or MITM); don't
    crash the worker with a ``KeyError`` — log and let the caller retry."""
    for key in ("certificate", "private_key"):
        if not data.get(key):
            raise ValueError(f"missing {key!r}")


def _install_cert(settings: Settings, data: dict[str, Any]) -> None:
    # Imported lazily so unit tests can mock the helper before the docker
    # SDK initializes (``apps/utils/docker/base`` creates a client at import).
    from apps.utils.docker.base import copy_file_into_container

    # Write key before cert so _client_auth's "cert exists" precondition
    # implies "key is durable". All writes are atomic via tmp+rename.
    _write_local_cert(settings, _KEY_FILE, data["private_key"], mode=0o600)
    _write_local_cert(settings, _CERT_FILE, data["certificate"])
    if "issuing_ca" in data:
        _write_local_cert(settings, _CA_FILE, data["issuing_ca"])

    nginx = settings.docker_nginx_name
    copy_file_into_container(nginx, "/root", "pem.crt", data["certificate"])
    copy_file_into_container(nginx, "/root", "cert.key", data["private_key"])
    if "issuing_ca" in data:
        copy_file_into_container(nginx, "/root", "ca.pem", data["issuing_ca"])


def _fetch_and_store_crl(settings: Settings) -> None:
    """Fetch CRL from manager, copy into nginx container. Shared with
    ``crl_sync_worker`` (see ``app/workers/crl.py``)."""
    from apps.utils.docker.base import copy_file_into_container

    try:
        res = requests.get(
            f"{settings.greffon_base_server}/api/greffer/ca/crl/",
            timeout=10,
            **_client_auth(settings),
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
