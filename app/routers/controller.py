"""Controller routes — manager-facing greffon lifecycle.

These endpoints mirror ``apps/controller/views.py`` exactly. The legacy
Django runtime still serves the same paths on the feature branch; nothing
routes real traffic to this FastAPI router until feature #4's cutover.

Handlers are plain ``def`` (not ``async def``) per the HLD #1 threading
contract: they call the sync Docker SDK and ``subprocess.Popen``, which
would block the event loop if the handler were declared async. FastAPI
runs sync handlers in a threadpool automatically.
"""
from __future__ import annotations

import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.auth import require_token
from app.models.controller import (
    GreffonStartRequest,
    GreffonStartResponse,
    GreffonStatusResponse,
    GreffonStopRequest,
    GreffonStopResponse,
)
from app.tunnel_config import TunnelConfigWriteError, maybe_write_client_toml

# Framework-agnostic shared code imported directly — no rewrite.
from apps.utils.docker import compose
from apps.utils.greffon import repository
from apps.utils.nginx import conf

logger = logging.getLogger("greffer")

# Time budget for ``_wait_for_compose_running`` after ``compose.start``
# returns. ``compose.start`` is fire-and-forget (``subprocess.Popen``
# without ``wait``), so we have to poll ``compose.get_status`` to know
# when nginx has actually bound the user-facing port. 10s covers
# already-pulled images by a wide margin; cold-pull misses the budget
# and we write client.toml anyway, relying on rathole-client's
# reconnect-on-failure behavior to bridge the brief gap. Codex P1 on
# greffer#25 caught the race.
_COMPOSE_READY_TIMEOUT_SECONDS = 10.0
_COMPOSE_READY_POLL_INTERVAL_SECONDS = 0.5

router = APIRouter(
    prefix="/api/controller",
    dependencies=[Depends(require_token)],
)


def _settings(request: Request):
    return request.app.state.settings


@router.post("/start/")
def start_greffon(
    payload: GreffonStartRequest, request: Request
) -> GreffonStartResponse:
    # Plain ``model_dump()``. ``configurations``/``ports`` have
    # ``default_factory`` on the model so an omitted key becomes an empty
    # container (not None and not absent), matching the strict vs safe
    # access patterns in apps/utils/greffon/repository.py
    # (``greffon['configurations']``) and apps/utils/docker/compose.py
    # (``.get('configurations', [])``). Explicit ``null`` is rejected by
    # Pydantic on type grounds.
    #
    # ``model_dump()`` also includes ``tunnel_client_toml`` — but the
    # downstream compose / repository code only reads keys it knows
    # about, so the extra field is harmless to pass through.
    greffon = payload.model_dump()
    compose_file = repository.get_compose_file_from_repository(greffon)
    greffon_info = repository.get_greffon_info(compose_file, greffon)
    compose_template = compose.get_compose_template(compose_file, greffon_info)
    compose.apply_configuration(greffon_info, compose_file)
    compose.create_compose(compose_template, greffon_info)
    conf.create_nginx_conf(greffon_info)
    compose.create_volumes_then_copy_files(greffon_info)
    compose.start(greffon_info)

    # v3 push race fix: compose.start uses subprocess.Popen and returns
    # before docker-compose has actually brought up the containers and
    # bound the user-facing port. Writing client.toml at this point
    # would let rathole-client's file-watcher pick up a config that
    # points at a not-yet-listening backend; rathole-client would
    # forward → connection refused → user sees a transient 502 until
    # rathole-client retries. Wait for compose to report 'running'
    # before writing. Bounded timeout — on slow/stuck images we write
    # anyway and rely on rathole-client's reconnect to bridge the gap.
    _wait_for_compose_running(greffon_info["id"])
    config_write_status = _write_pushed_client_toml(payload, request)

    return GreffonStartResponse(
        ports=greffon_info["ports"],
        config_write_status=config_write_status,
    )


@router.post("/stop/")
def stop_greffon(
    payload: GreffonStopRequest, request: Request
) -> GreffonStopResponse:
    compose.stop(payload.model_dump())

    # v3 push: write the manager-rendered client.toml AFTER the
    # container is stopped. The dropped server.toml service on the
    # manager side is what actually severs traffic; the greffer-side
    # file write here just lets rathole-client close its idle
    # forwarding pair. A failure is non-fatal — the manager logs it
    # and the next start will re-push the latest content.
    config_write_status = _write_pushed_client_toml(payload, request)

    return GreffonStopResponse(config_write_status=config_write_status)


def _wait_for_compose_running(greffon_id: str) -> None:
    """Poll compose.get_status until containers are running, or timeout.

    Bounded by _COMPOSE_READY_TIMEOUT_SECONDS. Logs a warning on timeout
    and returns — the caller writes client.toml anyway and rathole-
    client's reconnect-on-failure handles the brief gap. Never raises:
    a transient docker-socket error during polling counts as "still
    starting" and the loop continues.
    """
    deadline = time.monotonic() + _COMPOSE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status = compose.get_status(greffon_id)
        except Exception as exc:  # docker socket issues, race with stop, etc.
            logger.debug(
                "compose_status_transient_error greffon_id=%s err=%s",
                greffon_id, exc,
            )
            time.sleep(_COMPOSE_READY_POLL_INTERVAL_SECONDS)
            continue
        if status.get("status") == "running":
            logger.info(
                "compose_ready greffon_id=%s elapsed=%.2fs",
                greffon_id,
                _COMPOSE_READY_TIMEOUT_SECONDS - max(0, deadline - time.monotonic()),
            )
            return
        time.sleep(_COMPOSE_READY_POLL_INTERVAL_SECONDS)
    logger.warning(
        "compose_not_ready_within_timeout greffon_id=%s timeout=%.1fs — "
        "writing client.toml anyway; rathole-client will reconnect "
        "once backend binds",
        greffon_id, _COMPOSE_READY_TIMEOUT_SECONDS,
    )


def _write_pushed_client_toml(
    payload: GreffonStartRequest | GreffonStopRequest,
    request: Request,
) -> str:
    """Shared helper used by both start and stop handlers.

    Returns ``"ok"`` or ``"failed"`` matching the response model's
    ``ConfigWriteStatus`` literal. Never raises — even an unexpected
    exception (other than the documented OSError chain) is mapped to
    ``"failed"`` so the start/stop response shape stays predictable.
    """
    settings = _settings(request)
    target = settings.greffer_tunnel_client_config_path
    try:
        wrote = maybe_write_client_toml(payload.tunnel_client_toml, target)
    except TunnelConfigWriteError:
        # Already logged inside the helper; surface to manager.
        return "failed"
    except Exception:  # pragma: no cover — paranoid wrap
        logger.exception("tunnel_client_toml_write_unexpected_error")
        return "failed"
    if wrote:
        logger.debug("tunnel_client_toml_pushed_for_id=%s",
                     getattr(payload, "id", "?"))
    return "ok"


@router.get("/greffon/{greffon_id}/")
def greffon_status(greffon_id: UUID) -> GreffonStatusResponse:
    # Legacy Django view calls ``compose.status`` which does not exist —
    # it's always thrown AttributeError in prod. The correct function is
    # ``get_status`` (the monitoring thread uses it correctly). Fixed here
    # as part of the port; see hld-api-parity.md § Latent bug.
    result = compose.get_status(str(greffon_id))
    return GreffonStatusResponse(**result)
