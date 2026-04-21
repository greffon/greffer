from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# Defense-in-depth: restrict `id` to safe filename-like characters. The id
# is path-joined with $GREFFON_PATH inside the shared compose utilities, so
# a compromised/buggy manager sending `"../.."` would escape the data root.
# The trust boundary assumes the manager is trusted; this constraint costs
# nothing and covers all legitimate payloads (UUIDs + existing
# `test-instance-*` names in tests).
_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class Certificate(BaseModel):
    # Django serializer misspells this as "Cerificate"; internal-only,
    # never serialized by name. Pydantic side uses the correct spelling.
    certificate: str
    private_key: str


class GreffonField(BaseModel):
    value: Any
    destinations: Any


class GreffonStartRequest(BaseModel):
    # `id` is a free-form str in DRF (not UUID), matching what the manager
    # sends today (e.g. "test-instance-123"). Pattern kept permissive to
    # accept UUIDs and existing ID formats; rejects path-traversal.
    id: str = Field(pattern=_ID_PATTERN, min_length=1, max_length=128)
    repository_url: str
    cert: Certificate
    configurations: list[GreffonField] | None = None
    ports: dict[str, Any] | None = None


class GreffonStopRequest(BaseModel):
    id: str = Field(pattern=_ID_PATTERN, min_length=1, max_length=128)


class GreffonStartResponse(BaseModel):
    ports: list[Any]


class GreffonStatusResponse(BaseModel):
    status: str
    containers: list[dict[str, Any]]
