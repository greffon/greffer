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

from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth import require_token
from app.models.controller import (
    GreffonStartRequest,
    GreffonStartResponse,
    GreffonStatusResponse,
    GreffonStopRequest,
)

# Framework-agnostic shared code imported directly — no rewrite.
from apps.utils.docker import compose
from apps.utils.greffon import repository
from apps.utils.nginx import conf

router = APIRouter(
    prefix="/api/controller",
    dependencies=[Depends(require_token)],
)


@router.post("/start/")
def start_greffon(payload: GreffonStartRequest) -> GreffonStartResponse:
    # ``exclude_unset=True`` mirrors DRF's missing-vs-explicit distinction:
    # omitted optional fields stay out of the dict entirely, so downstream
    # ``greffon.get('ports', {}).get(...)`` in apps/utils/greffon/repository.py
    # and ``greffon_info.get('configurations', [])`` in compose.py see their
    # defaults. With plain ``model_dump()`` those sites would ``None.get(...)``
    # → AttributeError → 500 for every start request that omits the field.
    # Explicit ``null`` is rejected upstream by ``_reject_explicit_null``
    # (see app/models/controller.py).
    greffon = payload.model_dump(exclude_unset=True)
    compose_file = repository.get_compose_file_from_repository(greffon)
    greffon_info = repository.get_greffon_info(compose_file, greffon)
    compose_template = compose.get_compose_template(compose_file, greffon_info)
    compose.apply_configuration(greffon_info, compose_file)
    compose.create_compose(compose_template, greffon_info)
    conf.create_nginx_conf(greffon_info)
    compose.create_volumes_then_copy_files(greffon_info)
    compose.start(greffon_info)
    return GreffonStartResponse(ports=greffon_info["ports"])


@router.post("/stop/")
def stop_greffon(payload: GreffonStopRequest) -> dict:
    compose.stop(payload.model_dump())
    return {}


@router.get("/greffon/{greffon_id}/")
def greffon_status(greffon_id: UUID) -> GreffonStatusResponse:
    # Legacy Django view calls ``compose.status`` which does not exist —
    # it's always thrown AttributeError in prod. The correct function is
    # ``get_status`` (the monitoring thread uses it correctly). Fixed here
    # as part of the port; see hld-api-parity.md § Latent bug.
    result = compose.get_status(str(greffon_id))
    return GreffonStatusResponse(**result)
