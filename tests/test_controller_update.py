"""Tests for POST /api/controller/update/ (greffer self-update v2).

The spawn module (docker SDK) is patched, so no real docker. Focus: the
fail-closed gating (flag off -> 403, image unset -> 503), the tag-grammar 422,
the spawn-failure 500, and the 202 happy path with the updater wired from
settings.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import TOKEN_HEADER
from app.main import create_app
from app.settings import get_settings
from apps.utils.docker import updater as updater_spawn

_PINNED = "greffon/greffer-updater@sha256:" + "a" * 64


def _client(settings) -> AsyncClient:
    app = create_app(token="test-token", settings=settings)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _settings(monkeypatch, tmp_path, *, enabled=True, image=_PINNED):
    monkeypatch.setenv("GREFFER_ID", "g1")
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    # Set explicitly both ways: the flag now defaults ON, so the disabled case
    # must actively set it false rather than rely on an absent env var.
    monkeypatch.setenv("GREFFER_REMOTE_UPDATE_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("GREFFER_UPDATER_IMAGE", image)
    monkeypatch.setenv("GREFFER_VERSION_MANIFEST_URL", "https://x/m.json")
    return get_settings()


@pytest.mark.asyncio
async def test_update_disabled_returns_403(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, enabled=False)
    with patch.object(updater_spawn, "spawn_updater") as spawn:
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 403
    assert r.json()["detail"] == "remote_update_disabled"
    spawn.assert_not_called()  # gated at the source, nothing spawned


@pytest.mark.asyncio
async def test_update_image_unset_returns_503(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, image="")
    with patch.object(updater_spawn, "spawn_updater") as spawn:
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 503
    assert r.json()["detail"] == "updater_image_not_configured"
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_update_happy_spawns_and_returns_202(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with patch.object(updater_spawn, "spawn_updater", return_value="cid123") as spawn:
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 202
    body = r.json()
    assert body == {"status": "accepted", "updater_id": "cid123"}
    spawn.assert_called_once()
    kw = spawn.call_args.kwargs
    assert kw["image"] == _PINNED
    assert kw["target_tag"] == "0.3.6"
    assert kw["greffer_id"] == "g1"
    assert kw["data_dest"] == str(tmp_path)
    # socket model: the manifest_url / mode params are gone (the updater no longer
    # uses a manifest, and discovers the stack by label, not by mode)
    assert "manifest_url" not in kw and "mode" not in kw


@pytest.mark.asyncio
async def test_update_refused_409_when_update_in_progress(monkeypatch, tmp_path) -> None:
    # HLD section 10: a second remote update while one is recreating the stack is
    # refused fast (the /data lock is held) rather than spawning a doomed updater.
    settings = _settings(monkeypatch, tmp_path)
    with patch.object(updater_spawn, "spawn_updater") as spawn, \
            patch.object(updater_spawn, "update_in_progress", return_value=True):
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 409
    assert r.json()["detail"] == "update_in_progress"
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_update_unpinned_image_returns_503(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, image="greffon/greffer-updater:latest")
    with patch.object(updater_spawn, "spawn_updater") as spawn:
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 503
    assert r.json()["detail"] == "updater_image_not_digest_pinned"
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["bad:tag", "0.3.6\n", "../evil", "-leading", ""])
async def test_update_bad_tag_rejected(monkeypatch, tmp_path, bad) -> None:
    # The greffer maps RequestValidationError to 400 (app/errors.py), so a tag
    # failing the model grammar is a 400 before the handler runs.
    settings = _settings(monkeypatch, tmp_path)
    with patch.object(updater_spawn, "spawn_updater") as spawn:
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": bad},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 400
    spawn.assert_not_called()  # rejected by the model before the handler runs


@pytest.mark.asyncio
async def test_update_spawn_failure_returns_500(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with patch.object(updater_spawn, "spawn_updater",
                      side_effect=updater_spawn.UpdaterSpawnError("no socket")):
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"},
                              headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 500
    assert r.json()["detail"] == "updater_spawn_failed"


@pytest.mark.asyncio
async def test_update_requires_token(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with patch.object(updater_spawn, "spawn_updater") as spawn:
        async with _client(settings) as ac:
            r = await ac.post("/api/controller/update/", json={"target_tag": "0.3.6"})
    assert r.status_code in (401, 403)
    spawn.assert_not_called()
