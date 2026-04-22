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

logger = logging.getLogger("greffer")


_REGISTER_RETRY_SECONDS = 3.0
_CERT_POLL_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 10.0


async def register_worker(app: FastAPI) -> None:
    """Register with the manager and block until cert is installed.

    All ``anyio.to_thread.run_sync`` calls use ``abandon_on_cancel=True`` so
    that lifespan shutdown doesn't block waiting for a hung HTTP call — the
    async task returns immediately on cancel and the thread finishes (or
    gets killed at process exit) in the background. Every HTTP call also
    carries a ``timeout`` so the thread can't hang forever in the first
    place.
    """
    settings: Settings = app.state.settings
    token: str = app.state.greffer_token

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
            await anyio.to_thread.run_sync(
                _install_cert, settings, data, abandon_on_cancel=True
            )
            await anyio.to_thread.run_sync(
                _fetch_and_store_crl, settings, abandon_on_cancel=True
            )
            logger.info("register complete")
            return
        await asyncio.sleep(_CERT_POLL_SECONDS)


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
        verify=settings.greffer_ssl_verify,
        # Timeout is a safety net so the thread can't hang forever on a
        # stalled manager. Paired with abandon_on_cancel=True on the caller
        # side so shutdown is snappy regardless.
        timeout=_HTTP_TIMEOUT_SECONDS,
    )


def _fetch_cert(settings: Settings) -> dict[str, Any] | None:
    res = requests.get(
        f"{settings.greffon_base_server}/api/greffer/certificate/{settings.greffer_id}/",
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    return res.json() if res.status_code == 200 else None


def _install_cert(settings: Settings, data: dict[str, Any]) -> None:
    # Imported lazily so unit tests can mock the helper before the docker
    # SDK initializes (``apps/utils/docker/base`` creates a client at import).
    from apps.utils.docker.base import copy_file_into_container

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
