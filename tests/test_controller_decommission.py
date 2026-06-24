from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.auth import TOKEN_HEADER

_ID = "decomm-instance-1"
_AUTH = {TOKEN_HEADER: "test-token"}


@pytest.mark.asyncio
async def test_decommission_tears_down_volumes_and_dir(client: AsyncClient) -> None:
    """down -v, prune the <id>_ volumes, drop the dir, verify clean -> 200 with
    the removed volume names."""
    with patch("app.routers.controller.compose") as mc, patch(
        "app.routers.controller.volume"
    ) as mv, patch("app.routers.controller.shutil") as msh, patch(
        "app.routers.controller.os.path.exists", return_value=False
    ):
        mc.down.return_value = None
        mv.remove_instance_volumes.return_value = [f"{_ID}_data", f"{_ID}_db"]
        mv.list_instance_volumes.return_value = []  # verify: nothing residual
        r = await client.post(
            "/api/controller/decommission/", json={"id": _ID}, headers=_AUTH)

    assert r.status_code == 200
    assert r.json()["removed_volumes"] == [f"{_ID}_data", f"{_ID}_db"]
    mc.down.assert_called_once_with(_ID)
    mv.remove_instance_volumes.assert_called_once_with(_ID)
    # the instance dir (<GREFFON_PATH>/<id>) is removed, swallow-errors
    msh.rmtree.assert_called_once()
    rm_arg, rm_kw = msh.rmtree.call_args
    assert os.path.basename(rm_arg[0]) == _ID
    assert rm_kw == {"ignore_errors": True}


@pytest.mark.asyncio
async def test_decommission_surviving_dir_is_500(client: AsyncClient) -> None:
    """The instance dir survives the rmtree (busy mount / perm) -> the
    completeness verify must fail loud, not report a false success."""
    with patch("app.routers.controller.compose") as mc, patch(
        "app.routers.controller.volume"
    ) as mv, patch("app.routers.controller.shutil"), patch(
        "app.routers.controller.os.path.exists", return_value=True  # dir remains
    ):
        mc.down.return_value = None
        mv.remove_instance_volumes.return_value = []
        mv.list_instance_volumes.return_value = []  # no residual volumes
        r = await client.post(
            "/api/controller/decommission/", json={"id": _ID}, headers=_AUTH)

    assert r.status_code == 500
    assert r.json()["detail"] == "decommission_incomplete"


@pytest.mark.asyncio
async def test_decommission_is_idempotent_when_nothing_exists(client: AsyncClient) -> None:
    """An already-gone instance (no compose file, no volumes) is a 200 no-op."""
    with patch("app.routers.controller.compose") as mc, patch(
        "app.routers.controller.volume"
    ) as mv, patch("app.routers.controller.shutil"), patch(
        "app.routers.controller.os.path.exists", return_value=False
    ):
        mc.down.return_value = None  # no compose file
        mv.remove_instance_volumes.return_value = []
        mv.list_instance_volumes.return_value = []
        r = await client.post(
            "/api/controller/decommission/", json={"id": _ID}, headers=_AUTH)

    assert r.status_code == 200
    assert r.json()["removed_volumes"] == []


@pytest.mark.asyncio
async def test_decommission_residual_volume_is_500(client: AsyncClient) -> None:
    """A volume that survives the force-rm (e.g. still in use) must fail loud
    instead of silently leaking -> 500 decommission_incomplete."""
    with patch("app.routers.controller.compose"), patch(
        "app.routers.controller.volume"
    ) as mv, patch("app.routers.controller.shutil"):
        mv.remove_instance_volumes.return_value = [f"{_ID}_data"]
        mv.list_instance_volumes.return_value = [f"{_ID}_data"]  # still there
        r = await client.post(
            "/api/controller/decommission/", json={"id": _ID}, headers=_AUTH)

    assert r.status_code == 500
    assert r.json()["detail"] == "decommission_incomplete"


@pytest.mark.asyncio
async def test_decommission_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/api/controller/decommission/", json={"id": _ID})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_decommission_refuses_during_self_update(client: AsyncClient) -> None:
    with patch("app.routers.controller.updater_spawn") as mu, patch(
        "app.routers.controller.compose"
    ), patch("app.routers.controller.volume"), patch("app.routers.controller.shutil"):
        mu.update_in_progress.return_value = True
        r = await client.post(
            "/api/controller/decommission/", json={"id": _ID}, headers=_AUTH)

    assert r.status_code == 409
    assert r.json()["detail"] == "update_in_progress"


@pytest.mark.asyncio
async def test_decommission_409_when_instance_busy(client: AsyncClient) -> None:
    """A start/stop/backup holding the per-instance lock -> 409 instance_busy
    (the decommission must not race a concurrent op on the same instance)."""
    from app import backup

    lock = backup._instance_lock(_ID)
    assert lock.acquire(blocking=False)
    try:
        with patch("app.routers.controller.compose"), patch(
            "app.routers.controller.volume"
        ), patch("app.routers.controller.shutil"):
            r = await client.post(
                "/api/controller/decommission/", json={"id": _ID}, headers=_AUTH)
        assert r.status_code == 409
        assert r.json()["detail"] == "instance_busy"
    finally:
        lock.release()
