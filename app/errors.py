from __future__ import annotations

from typing import Any, Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse


def _drf_shape(errors: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Convert pydantic error list to DRF-style ``{"field": ["msg", ...]}``.

    Pydantic returns errors of the form::

        {"loc": ("body", "cert", "certificate"), "msg": "Field required", ...}

    The ``body`` / ``path`` / ``query`` prefix is a FastAPI artifact that
    identifies where the error came from. We drop these so the resulting
    key matches the field name the manager sent (body) or the path
    parameter name (path). Nested body fields become dotted keys (e.g.
    ``cert.certificate``) — the manager today does not introspect nested
    error structures.
    """
    LOC_PREFIXES_TO_STRIP = {"body", "path", "query"}
    out: dict[str, list[str]] = {}
    for err in errors:
        loc = [str(p) for p in err.get("loc", ()) if p not in LOC_PREFIXES_TO_STRIP]
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


async def _http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Preserve Django contract on 401: body is ``{}``, not ``{"detail": ...}``.

    The legacy ``@is_logged`` decorator returned ``JsonResponse({}, status=401)``.
    FastAPI's default serializes ``HTTPException(status_code=401, detail=...)``
    as ``{"detail": "..."}`` which is a silent byte-level contract change. We
    special-case 401 to match Django; everything else delegates to FastAPI's
    default handler so we inherit its behavior for 404, 405, 500, etc.
    """
    if exc.status_code == 401:
        return JSONResponse(status_code=401, content={})
    return await http_exception_handler(request, exc)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
