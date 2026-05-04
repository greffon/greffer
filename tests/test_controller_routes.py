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
async def test_start_defaults_unset_optional_fields_to_empty_containers(
    client: AsyncClient,
) -> None:
    """REGRESSION: the dict passed to the downstream orchestration code
    must carry ``configurations`` and ``ports`` as empty containers (not
    absent, not None) when the client omits them:

      - ``apps/utils/greffon/repository.py:create_greffon_info`` does
        strict ``greffon['configurations']`` → KeyError if absent.
      - ``apps/utils/greffon/repository.py:create_greffon_info`` reads
        ``greffon.get('ports', {}).get(port_name, {}).get('url')`` —
        tolerates missing, but None.get(...) would raise.

    The Pydantic model uses ``default_factory=list/dict`` to give the
    dump predictable shape on these paths.
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
    dict_passed = mock_repo.get_compose_file_from_repository.call_args[0][0]
    assert dict_passed["configurations"] == []
    assert dict_passed["ports"] == {}


@pytest.mark.asyncio
async def test_start_rejects_blank_repository_url(client: AsyncClient) -> None:
    """Blank repository_url must 400, not reach ``requests.get('')`` and 500."""
    payload = {**SAMPLE_START_PAYLOAD, "repository_url": ""}
    r = await client.post(
        "/api/controller/start/",
        json=payload,
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Invalid Fields"
    assert "repository_url" in body["errors"]


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
    # v3 stop response carries config_write_status so the manager can
    # surface greffer-side push failures. With no tunnel_client_toml in
    # the request body (proxy-mode greffer / v2 manager), nothing was
    # written and the status is 'ok' — but the field is always present.
    assert r.json() == {"config_write_status": "ok"}
    mock_compose.stop.assert_called_once_with(
        {"id": "test-instance-123", "tunnel_client_toml": None}
    )


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


# ---------------------------------------------------------------------------
# v3 push: tunnel_client_toml in start/stop request bodies
#
# These exercise the manager → greffer push path added in tunnel-support
# epic v3. The shared writer is unit-tested in test_tunnel_config.py;
# here we verify the router wires it in correctly: file is written when
# the field is present, response carries config_write_status, write
# failures are surfaced as ``failed`` rather than 500.
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_compose_repo_conf():
    """Stub out the docker compose / repo / nginx side so the tunnel-
    config tests below don't require a real docker daemon."""
    with patch("app.routers.controller.repository") as mock_repo, patch(
        "app.routers.controller.compose"
    ) as mock_compose, patch("app.routers.controller.conf") as mock_conf:
        mock_repo.get_compose_file_from_repository.return_value = {
            "services": {"app": {"image": "nginx"}}
        }
        mock_repo.get_greffon_info.return_value = {
            "ports": [{"port_host": 9000, "port_name": "app_80"}],
            "id": "test-instance-123",
        }
        mock_compose.get_compose_template.return_value = {}
        yield mock_repo, mock_compose, mock_conf


@pytest.mark.asyncio
async def test_start_omits_tunnel_field_returns_ok(
    client: AsyncClient, patch_compose_repo_conf
) -> None:
    """Proxy-mode greffer / v2 manager — payload has no
    ``tunnel_client_toml``. Handler skips the file write and returns
    config_write_status='ok' (nothing failed because nothing was
    attempted)."""
    r = await client.post(
        "/api/controller/start/",
        json=SAMPLE_START_PAYLOAD,
        headers={TOKEN_HEADER: "test-token"},
    )
    assert r.status_code == 200
    assert r.json()["config_write_status"] == "ok"


@pytest.mark.asyncio
async def test_start_with_tunnel_field_writes_file(
    client: AsyncClient, patch_compose_repo_conf, tmp_path
) -> None:
    """Tunnel-mode greffer + v3 manager — payload carries
    ``tunnel_client_toml``. Handler writes it atomically to the
    configured path and returns ``ok``."""
    target = tmp_path / "client.toml"
    payload = {**SAMPLE_START_PAYLOAD, "tunnel_client_toml": "[client]\n"}

    with patch.object(
        client._transport.app.state.settings,
        "greffer_tunnel_client_config_path",
        str(target),
    ):
        r = await client.post(
            "/api/controller/start/",
            json=payload,
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    assert r.json()["config_write_status"] == "ok"
    assert target.read_text() == "[client]\n"


@pytest.mark.asyncio
async def test_start_with_tunnel_field_failure_returns_failed(
    client: AsyncClient, patch_compose_repo_conf, tmp_path
) -> None:
    """Tunnel-mode greffer + v3 manager + filesystem error (e.g.
    parent directory missing) — handler returns 200 with
    config_write_status='failed', NOT a 500. Manager surfaces the
    failed status to the API caller; instance start itself succeeded."""
    bogus_target = tmp_path / "does-not-exist" / "client.toml"
    payload = {**SAMPLE_START_PAYLOAD, "tunnel_client_toml": "[client]\n"}

    with patch.object(
        client._transport.app.state.settings,
        "greffer_tunnel_client_config_path",
        str(bogus_target),
    ):
        r = await client.post(
            "/api/controller/start/",
            json=payload,
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    assert r.json()["config_write_status"] == "failed"


@pytest.mark.asyncio
async def test_stop_with_tunnel_field_writes_file(
    client: AsyncClient, tmp_path
) -> None:
    """Stop path mirrors start: tunnel_client_toml is consumed and
    config_write_status surfaced. Stop typically pushes a file with
    the stopping instance's services removed (filtered server-side
    by the GREFFON_STOPPING status check in render_client_toml)."""
    target = tmp_path / "client.toml"

    with patch("app.routers.controller.compose") as mock_compose, patch.object(
        client._transport.app.state.settings,
        "greffer_tunnel_client_config_path",
        str(target),
    ):
        mock_compose.stop.return_value = None
        r = await client.post(
            "/api/controller/stop/",
            json={
                "id": "test-instance-123",
                "tunnel_client_toml": "[client]\nservices=[]\n",
            },
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    assert r.json()["config_write_status"] == "ok"
    assert target.read_text() == "[client]\nservices=[]\n"


@pytest.mark.asyncio
async def test_stop_omits_tunnel_field_returns_ok(
    client: AsyncClient, tmp_path
) -> None:
    """Proxy mode / v2 manager — stop payload has no tunnel field.
    Handler returns ok without writing anything."""
    with patch("app.routers.controller.compose") as mock_compose:
        mock_compose.stop.return_value = None
        r = await client.post(
            "/api/controller/stop/",
            json={"id": "test-instance-123"},
            headers={TOKEN_HEADER: "test-token"},
        )

    assert r.status_code == 200
    assert r.json()["config_write_status"] == "ok"
