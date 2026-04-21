"""Tests for the DRF-compatible validation error handler.

FastAPI's default is 422 with a pydantic-native error body. The manager
expects DRF's 400 with ``{"message": "Invalid Fields", "errors": {...}}``.
These tests lock in the top-level contract. Deep-nested error key shape
is a documented drift (dotted keys vs nested dicts) and is NOT locked in
here — see hld-api-parity.md.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.auth import TOKEN_HEADER
from app.errors import _drf_shape


# ---------------------------------------------------------------------------
# Handler unit tests (no HTTP)
# ---------------------------------------------------------------------------


def test_drf_shape_strips_body_prefix() -> None:
    errs = [
        {"loc": ("body", "id"), "msg": "Field required"},
    ]
    assert _drf_shape(errs) == {"id": ["Field required"]}


def test_drf_shape_groups_multiple_messages_per_field() -> None:
    errs = [
        {"loc": ("body", "id"), "msg": "msg1"},
        {"loc": ("body", "id"), "msg": "msg2"},
    ]
    assert _drf_shape(errs) == {"id": ["msg1", "msg2"]}


def test_drf_shape_dotted_nested_keys() -> None:
    errs = [
        {"loc": ("body", "cert", "certificate"), "msg": "Field required"},
    ]
    assert _drf_shape(errs) == {"cert.certificate": ["Field required"]}


def test_drf_shape_handles_empty_loc() -> None:
    assert _drf_shape([{"loc": (), "msg": "top-level"}]) == {"_": ["top-level"]}


def test_drf_shape_handles_path_param() -> None:
    """For path-param errors, loc = ('path', 'greffon_id') — no 'body' prefix."""
    errs = [
        {"loc": ("path", "greffon_id"), "msg": "Invalid UUID"},
    ]
    assert _drf_shape(errs) == {"path.greffon_id": ["Invalid UUID"]}


# ---------------------------------------------------------------------------
# End-to-end shape (through FastAPI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_error_returns_400_with_drf_shape(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/api/controller/stop/",
        json={},  # missing `id`
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body.keys() == {"message", "errors"}
    assert body["message"] == "Invalid Fields"
    assert isinstance(body["errors"], dict)
    assert "id" in body["errors"]
    assert isinstance(body["errors"]["id"], list)


@pytest.mark.asyncio
async def test_validation_error_for_nested_field(client: AsyncClient) -> None:
    """Missing cert.certificate → key is dotted (`cert.certificate`)."""
    r = await client.post(
        "/api/controller/start/",
        json={
            "id": "x",
            "repository_url": "u",
            "cert": {"private_key": "k"},  # missing certificate
        },
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"
    assert "cert.certificate" in body["errors"]


@pytest.mark.asyncio
async def test_validation_error_path_param_uuid(client: AsyncClient) -> None:
    r = await client.get(
        "/api/controller/greffon/not-a-uuid/",
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"
    # path errors have loc=('path', 'greffon_id')
    assert "path.greffon_id" in body["errors"]
