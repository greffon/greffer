from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.auth import TOKEN_HEADER


SAMPLE_CERT = {
    "certificate": "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----",
}

SAMPLE_START_PAYLOAD = {
    "id": "test-instance-123",
    "repository_url": "https://example.com/docker-compose.yml",
    "cert": SAMPLE_CERT,
    "configurations": [
        {
            "value": {"db_host": "localhost"},
            "destinations": [{"type": "json", "name": "config.json"}],
        }
    ],
    "ports": {"app_80": {"url": "https://field.greffon.io"}},
}


# ---------------------------------------------------------------------------
# POST /api/controller/start/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_success(client: AsyncClient) -> None:
    with patch("app.routers.controller.repository") as mock_repo, patch(
        "app.routers.controller.compose"
    ) as mock_compose, patch("app.routers.controller.conf") as mock_conf:
        mock_repo.get_compose_file_from_repository.return_value = {
            "services": {"app": {"image": "nginx"}}
        }
        mock_repo.get_greffon_info.return_value = {
            "ports": [
                {
                    "port_host": 9000,
                    "port_container": "80",
                    "container_name": "app",
                    "port_name": "app_80",
                    "url": "https://field.greffon.io",
                }
            ],
            "id": "test-instance-123",
        }
        mock_compose.get_compose_template.return_value = {}

        r = await client.post(
            "/api/controller/start/",
            json=SAMPLE_START_PAYLOAD,
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    body = r.json()
    assert "ports" in body
    assert body["ports"][0]["port_name"] == "app_80"

    mock_repo.get_compose_file_from_repository.assert_called_once()
    mock_repo.get_greffon_info.assert_called_once()
    mock_compose.get_compose_template.assert_called_once()
    mock_compose.apply_configuration.assert_called_once()
    mock_compose.create_compose.assert_called_once()
    mock_conf.create_nginx_conf.assert_called_once()
    mock_compose.create_volumes_then_copy_files.assert_called_once()
    mock_compose.start.assert_called_once()


@pytest.mark.asyncio
async def test_start_rejects_missing_fields(client: AsyncClient) -> None:
    r = await client.post(
        "/api/controller/start/",
        json={"invalid": "data"},
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"
    assert "errors" in body


@pytest.mark.asyncio
async def test_start_rejects_wrong_token(client: AsyncClient) -> None:
    r = await client.post(
        "/api/controller/start/",
        json=SAMPLE_START_PAYLOAD,
        headers={TOKEN_HEADER: "wrong-token"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_start_rejects_missing_token(client: AsyncClient) -> None:
    r = await client.post(
        "/api/controller/start/",
        json=SAMPLE_START_PAYLOAD,
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_start_401_body_is_empty_object(client: AsyncClient) -> None:
    """Preserve Django contract: 401 body is ``{}``, not ``{"detail": ...}``."""
    r = await client.post(
        "/api/controller/start/",
        json=SAMPLE_START_PAYLOAD,
    )
    assert r.status_code == 401
    assert r.json() == {}


@pytest.mark.asyncio
async def test_start_omits_unset_optional_fields_from_downstream_dict(
    client: AsyncClient,
) -> None:
    """REGRESSION: ``model_dump()`` without ``exclude_unset=True`` materializes
    omitted optional fields as ``None`` in the dict; downstream
    ``greffon.get('ports', {}).get(...)`` then calls ``None.get(...)`` →
    500. Verify the dict passed to the downstream orchestration code omits
    the keys entirely when the client did not send them.
    """
    payload = {
        "id": "test-instance-123",
        "repository_url": "https://example.com/docker-compose.yml",
        "cert": SAMPLE_CERT,
        # intentionally no "configurations", no "ports"
    }
    with patch("app.routers.controller.repository") as mock_repo, patch(
        "app.routers.controller.compose"
    ), patch("app.routers.controller.conf"):
        mock_repo.get_compose_file_from_repository.return_value = {}
        mock_repo.get_greffon_info.return_value = {"ports": [], "id": "x"}

        r = await client.post(
            "/api/controller/start/",
            json=payload,
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    # The call to downstream must NOT carry the optional keys as None.
    dict_passed = mock_repo.get_compose_file_from_repository.call_args[0][0]
    assert "configurations" not in dict_passed
    assert "ports" not in dict_passed


@pytest.mark.asyncio
async def test_start_accepts_empty_configurations_list(
    client: AsyncClient,
) -> None:
    """``configurations: []`` is semantically "no configs" and must succeed."""
    payload = {**SAMPLE_START_PAYLOAD, "configurations": []}
    with patch("app.routers.controller.repository") as mock_repo, patch(
        "app.routers.controller.compose"
    ), patch("app.routers.controller.conf"):
        mock_repo.get_compose_file_from_repository.return_value = {}
        mock_repo.get_greffon_info.return_value = {"ports": [], "id": "x"}

        r = await client.post(
            "/api/controller/start/",
            json=payload,
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_start_ignores_unknown_extra_fields(client: AsyncClient) -> None:
    """Pydantic default is ``extra="ignore"``. Locks that in — a future
    ``extra="forbid"`` change would silently reject manager traffic and this
    test would catch it."""
    payload = {**SAMPLE_START_PAYLOAD, "new_future_field": "whatever"}
    with patch("app.routers.controller.repository") as mock_repo, patch(
        "app.routers.controller.compose"
    ), patch("app.routers.controller.conf"):
        mock_repo.get_compose_file_from_repository.return_value = {}
        mock_repo.get_greffon_info.return_value = {"ports": [], "id": "x"}

        r = await client.post(
            "/api/controller/start/",
            json=payload,
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_start_rejects_explicit_null_configurations(
    client: AsyncClient,
) -> None:
    """Match DRF: explicit null is 400, not silently coerced to None."""
    payload = {**SAMPLE_START_PAYLOAD, "configurations": None}
    r = await client.post(
        "/api/controller/start/",
        json=payload,
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    assert r.json()["message"] == "Invalid Fields"
    assert "configurations" in r.json()["errors"]


@pytest.mark.asyncio
async def test_start_rejects_path_traversal_id(client: AsyncClient) -> None:
    """Defense-in-depth: ``id`` is path-joined with $GREFFON_PATH downstream.
    A payload with dots/slashes must be rejected at the validation layer."""
    payload = {**SAMPLE_START_PAYLOAD, "id": "../../etc/passwd"}
    r = await client.post(
        "/api/controller/start/",
        json=payload,
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"
    assert "id" in body["errors"]


# ---------------------------------------------------------------------------
# POST /api/controller/stop/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_success(client: AsyncClient) -> None:
    with patch("app.routers.controller.compose") as mock_compose:
        r = await client.post(
            "/api/controller/stop/",
            json={"id": "test-instance-123"},
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    assert r.json() == {}
    mock_compose.stop.assert_called_once_with({"id": "test-instance-123"})


@pytest.mark.asyncio
async def test_stop_rejects_missing_id(client: AsyncClient) -> None:
    r = await client.post(
        "/api/controller/stop/",
        json={},
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"


@pytest.mark.asyncio
async def test_stop_rejects_missing_token(client: AsyncClient) -> None:
    r = await client.post(
        "/api/controller/stop/",
        json={"id": "x"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/controller/greffon/{uuid}/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_success(client: AsyncClient) -> None:
    instance_id = uuid.uuid4()
    with patch("app.routers.controller.compose") as mock_compose:
        mock_compose.get_status.return_value = {
            "status": "running",
            "containers": [{"status": "running"}],
        }
        r = await client.get(
            f"/api/controller/greffon/{instance_id}/",
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["containers"] == [{"status": "running"}]
    mock_compose.get_status.assert_called_once_with(str(instance_id))


@pytest.mark.asyncio
async def test_status_preserves_unknow_typo(client: AsyncClient) -> None:
    """The upstream compose.get_status returns 'unknow' (sic) for mixed state.

    Tests lock this in so we don't silently "fix" it — the manager may
    pattern-match on the exact string.
    """
    instance_id = uuid.uuid4()
    with patch("app.routers.controller.compose") as mock_compose:
        mock_compose.get_status.return_value = {
            "status": "unknow",
            "containers": [
                {"status": "running"},
                {"status": "stopped"},
            ],
        }
        r = await client.get(
            f"/api/controller/greffon/{instance_id}/",
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    assert r.json()["status"] == "unknow"


@pytest.mark.asyncio
async def test_status_rejects_non_uuid(client: AsyncClient) -> None:
    r = await client.get(
        "/api/controller/greffon/not-a-uuid/",
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"


@pytest.mark.asyncio
async def test_status_rejects_missing_token(client: AsyncClient) -> None:
    r = await client.get(f"/api/controller/greffon/{uuid.uuid4()}/")
    assert r.status_code == 401
