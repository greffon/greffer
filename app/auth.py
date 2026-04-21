from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

TOKEN_HEADER = "X-GREFFON-TOKEN"

_token_scheme = APIKeyHeader(name=TOKEN_HEADER, auto_error=False)


def get_expected_token(request: Request) -> str:
    return request.app.state.greffer_token


async def require_token(
    provided: str | None = Depends(_token_scheme),
    expected: str = Depends(get_expected_token),
) -> None:
    if provided is None or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
