"""Tests for the FastAPI lifespan — gates worker startup on
``greffer_workers_enabled``.

Lifespan is exercised by driving the ``lifespan(app)`` async context
manager directly. Note: ``httpx.AsyncClient + ASGITransport`` does *not*
run lifespan events in the current httpx release, so going through the
HTTP client wouldn't trigger startup/shutdown.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.lifespan import lifespan
from app.main import create_app
from app.settings import Settings, get_settings
from app.workers import stop_workers


@pytest.mark.asyncio
async def test_lifespan_no_tasks_when_workers_disabled(
    settings: Settings,
) -> None:
    """Default ``greffer_workers_enabled=False`` → start_workers is not called."""
    assert settings.greffer_workers_enabled is False
    app = create_app(token="t", settings=settings)

    with patch("app.lifespan.start_workers") as mock_start, patch(
        "app.lifespan.stop_workers"
    ) as mock_stop:
        async with lifespan(app):
            pass

    mock_start.assert_not_called()
    mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_starts_three_workers_when_enabled(
    settings: Settings,
) -> None:
    """``greffer_workers_enabled=True`` → three tasks started with expected
    names, cancelled on shutdown."""
    settings.greffer_workers_enabled = True  # type: ignore[misc]
    app = create_app(token="t", settings=settings)

    async def _noop_worker(_app):
        await asyncio.sleep(3600)  # sleep forever; cancellable

    # Patch the bindings that `start_workers` uses — those are module-level
    # imports in `app/workers/__init__.py`, so patching `app.workers.X`
    # reaches them before `start_workers` looks them up.
    with patch("app.workers.register_worker", new=_noop_worker), patch(
        "app.workers.monitor_worker", new=_noop_worker
    ), patch("app.workers.crl_sync_worker", new=_noop_worker):
        async with lifespan(app):
            current_names = {
                t.get_name() for t in asyncio.all_tasks() if not t.done()
            }
            assert "greffer-register" in current_names
            assert "greffer-monitor" in current_names
            assert "greffer-crl-sync" in current_names

    # After lifespan shutdown the worker tasks must be gone.
    leftover = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert "greffer-register" not in leftover
    assert "greffer-monitor" not in leftover
    assert "greffer-crl-sync" not in leftover


def test_greffer_workers_enabled_env_var_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION (Codex P1 on #17): pydantic-settings maps field names
    (case-insensitive) to env vars. A bare ``workers_enabled`` field
    would bind to ``WORKERS_ENABLED``, silently ignoring the
    ``GREFFER_WORKERS_ENABLED`` env var the compose file sets — meaning
    the cutover would ship with workers DORMANT. This test confirms the
    field name carries the ``greffer_`` prefix so the env var lands.
    """
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.setenv("GREFFER_WORKERS_ENABLED", "true")
    get_settings.cache_clear()
    s = get_settings()
    assert s.greffer_workers_enabled is True


def test_greffer_workers_enabled_env_var_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against an accidental `default=True` flip."""
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.delenv("GREFFER_WORKERS_ENABLED", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.greffer_workers_enabled is False


def test_bare_workers_enabled_env_var_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric guard: ``WORKERS_ENABLED`` (no prefix) must NOT bind,
    because the field is ``greffer_workers_enabled``. If someone ever
    renames the field back to bare ``workers_enabled`` as "cleanup",
    this test flips red.
    """
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.setenv("WORKERS_ENABLED", "true")  # intentionally wrong name
    monkeypatch.delenv("GREFFER_WORKERS_ENABLED", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.greffer_workers_enabled is False


@pytest.mark.asyncio
async def test_lifespan_publishes_token_to_sidecar_volume(
    settings: Settings, tmp_path,
) -> None:
    """Lifespan startup writes the active greffer_token to the path
    configured in settings, with 0600 perms, atomically. The sidecar
    reads from the same path on its own mount of the shared volume."""
    target = tmp_path / 'greffer-token'
    settings.greffer_token_file_path = str(target)  # type: ignore[misc]
    app = create_app(token='shared-tok', settings=settings)
    async with lifespan(app):
        assert target.exists()
        assert target.read_text() == 'shared-tok'
        # 0600 — only the greffer process and the sidecar (via shared
        # volume mount) should ever see it.
        assert oct(target.stat().st_mode)[-3:] == '600'
        # No leftover tmp file — atomic rename, not partial write.
        assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.asyncio
async def test_lifespan_token_publish_disabled_when_path_empty(
    settings: Settings, tmp_path,
) -> None:
    settings.greffer_token_file_path = ''  # type: ignore[misc]
    app = create_app(token='ignored', settings=settings)
    async with lifespan(app):
        # No file should be created in tmp_path or anywhere else
        # detectable here. The behavior is "do nothing"; we just need
        # the lifespan to not raise.
        pass


@pytest.mark.asyncio
async def test_lifespan_token_publish_swallows_oserror(
    settings: Settings,
) -> None:
    """If the shared volume isn't mounted (proxy-mode misconfig, missing
    parent dir on a read-only fs), startup must continue. The sidecar
    is the only consumer; it backs off on auth failure. This test
    asserts non-fatal behavior: lifespan completes, no exception leaks."""
    # /dev/null/forbidden cannot be created (parent is not a directory).
    # mkdir(parents=True) raises NotADirectoryError, write_text never
    # runs. The function must catch and continue.
    settings.greffer_token_file_path = '/dev/null/forbidden/greffer-token'  # type: ignore[misc]
    app = create_app(token='t', settings=settings)
    # If the function let the OSError propagate, this would raise.
    async with lifespan(app):
        pass


def test_settings_greffer_token_env_var_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GREFFER_TOKEN env var binds to settings.greffer_token (operator
    explicit override path). Default is None so create_app mints one."""
    monkeypatch.setenv('GREFFER_ID', 'test')
    monkeypatch.setenv('GREFFER_TOKEN', 'operator-supplied-tok')
    get_settings.cache_clear()
    s = get_settings()
    assert s.greffer_token == 'operator-supplied-tok'


@pytest.mark.asyncio
async def test_stop_workers_cancels_and_awaits() -> None:
    """Unit test on the orchestration helper directly."""

    async def _sleeper():
        await asyncio.sleep(3600)

    tasks = [asyncio.create_task(_sleeper()) for _ in range(3)]
    await stop_workers(tasks)
    for t in tasks:
        assert t.done()
        assert t.cancelled()
