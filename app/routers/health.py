from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import require_token
from app.readiness import evaluate_readiness

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Health endpoint returning the greffer's identity alongside liveness.

    The ``id`` field is consumed by the greffer-cli's post-Connected
    reachability self-test (greffon/greffon#75 — greffer-cli epic) to
    distinguish "this hostname resolves to A live greffer" from "this
    hostname resolves to THE greffer the operator just installed."
    A typo'd ``--public-host`` that happens to point at another live
    greffer would return 200 here too — the CLI compares ``id`` against
    the GREFFER_ID it wrote into env.env.
    """
    return {
        "id": request.app.state.settings.greffer_id,
        "status": "ok",
    }


@router.get("/readyz", dependencies=[Depends(require_token)])
async def readyz(request: Request) -> JSONResponse:
    """Readiness with a fatal-vs-degraded split (greffer-observability epic,
    Feature #3).

    Returns **503** on a FATAL condition (docker daemon unreachable, a
    long-lived worker dead) so the compose healthcheck can surface it; **200**
    otherwise, including when ``degraded`` (a greffer pending acceptance is
    healthy and must not be flapped). The ``reasons`` list is machine-readable.

    Auth is ``X-GREFFON-TOKEN`` (same as the controller routes) because this
    endpoint is publicly routed like ``/healthz`` and would otherwise leak the
    greffer's internal state. The in-process watchdog shares
    ``evaluate_readiness`` so the endpoint and the self-heal decision can never
    drift. ``/healthz`` stays liveness-only (a greffer-cli contract).
    """
    r = evaluate_readiness(request.app)
    return JSONResponse(
        {
            "id": request.app.state.settings.greffer_id,
            "status": r.status,
            "reasons": r.reasons,
        },
        status_code=503 if r.fatal else 200,
    )
