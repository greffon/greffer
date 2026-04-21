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
    # `min_length=1` matches DRF's `CharField` default (rejects blank).
    # Otherwise an empty URL reaches `requests.get('')` and 500s instead
    # of returning a 400 validation error.
    repository_url: str = Field(min_length=1)
    cert: Certificate
    # Optional in the DRF sense (may be omitted from the payload), but
    # defaulted to an empty container here so the dumped dict always has
    # the key present. `create_greffon_info` in
    # apps/utils/greffon/repository.py uses strict `greffon['configurations']`
    # access, not `.get(...)`, so omitting the key → KeyError → 500.
    # Explicit `null` is rejected on type grounds (list, not list | None).
    configurations: list[GreffonField] = Field(default_factory=list)
    ports: dict[str, Any] = Field(default_factory=dict)


class GreffonStopRequest(BaseModel):
    id: str = Field(pattern=_ID_PATTERN, min_length=1, max_length=128)


class GreffonStartResponse(BaseModel):
    ports: list[Any]


class GreffonStatusResponse(BaseModel):
    status: str
    containers: list[dict[str, Any]]
