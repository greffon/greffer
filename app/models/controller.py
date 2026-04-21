from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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
    # sends today (e.g. "test-instance-123"). Kept loose deliberately.
    id: str
    repository_url: str
    cert: Certificate
    configurations: list[GreffonField] | None = None
    ports: dict[str, Any] | None = None


class GreffonStopRequest(BaseModel):
    id: str


class GreffonStartResponse(BaseModel):
    ports: list[Any]


class GreffonStatusResponse(BaseModel):
    status: str
    containers: list[dict[str, Any]]
