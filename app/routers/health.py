from __future__ import annotations

from fastapi import APIRouter, Request

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
