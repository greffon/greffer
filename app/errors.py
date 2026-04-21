from __future__ import annotations

from typing import Any, Iterable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _drf_shape(errors: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Convert pydantic error list to DRF-style ``{"field": ["msg", ...]}``.

    Pydantic returns errors of the form::

        {"loc": ("body", "cert", "certificate"), "msg": "Field required", ...}

    The ``body`` prefix is a FastAPI artifact that identifies where the
    error came from (body vs path vs query). We drop it so the resulting
    key matches the field name the manager sent. Nested fields become
    dotted keys (e.g. ``cert.certificate``) — the manager today does not
    introspect nested error structures.
    """
    out: dict[str, list[str]] = {}
    for err in errors:
        loc = [str(p) for p in err.get("loc", ()) if p != "body"]
        key = ".".join(loc) if loc else "_"
        out.setdefault(key, []).append(err.get("msg", ""))
    return out


async def _validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "message": "Invalid Fields",
            "errors": _drf_shape(exc.errors()),
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_handler)
