"""Controller routes — manager-facing greffon lifecycle.

These endpoints mirror ``apps/controller/views.py`` exactly. The legacy
Django runtime still serves the same paths on the feature branch; nothing
routes real traffic to this FastAPI router until feature #4's cutover.

Handlers are plain ``def`` (not ``async def``) per the HLD #1 threading
contract: they call the sync Docker SDK and ``subprocess.Popen``, which
would block the event loop if the handler were declared async. FastAPI
runs sync handlers in a threadpool automatically.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Literal
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import require_token
from app.diagnostics import diag
from app.log_context import instance_id_var
from app import backup
from app.models.controller import (
    GreffonBackupRequest,
    GreffonBackupResponse,
    GreffonRestoreRequest,
    GreffonRestoreResponse,
    GreffonStartRequest,
    GreffonStartResponse,
    GreffonStatusResponse,
    GreffonStopRequest,
    GreffonStopResponse,
    InstanceDiskResponse,
    InstanceLogsResponse,
    InstanceStatsResponse,
    RemoteUpdateRequest,
    RemoteUpdateResponse,
    TunnelConfigPushRequest,
    TunnelConfigPushResponse,
)
from app.tunnel_config import (
    TunnelConfigWriteError,
    maybe_write_client_toml,
    write_client_toml,
)

# Framework-agnostic shared code imported directly — no rewrite.
from apps.utils.docker import compose, instance_logs, l4_ports, observe
from apps.utils.docker import updater as updater_spawn
from apps.utils.greffon import repository
from apps.utils.nginx import conf

logger = logging.getLogger("greffer")

# Time budget for ``_wait_for_compose_running`` after ``compose.start``
# returns. ``compose.start`` is fire-and-forget (``subprocess.Popen``
# without ``wait``), so we have to poll ``compose.get_status`` to know
# when nginx has actually bound the user-facing port. 10s covers
# already-pulled images by a wide margin; cold-pull misses the budget
# and we write client.toml anyway, relying on rathole-client's
# reconnect-on-failure behavior to bridge the brief gap. Codex P1 on
# greffer#25 caught the race.
_COMPOSE_READY_TIMEOUT_SECONDS = 10.0
_COMPOSE_READY_POLL_INTERVAL_SECONDS = 0.5

router = APIRouter(
    prefix="/api/controller",
    dependencies=[Depends(require_token)],
)


def _settings(request: Request):
    return request.app.state.settings


def _refuse_if_updating(settings) -> None:
    """409 if a self-update is recreating the stack (HLD section 10): a manager
    start/stop/update must not race the updater, which during a recreate stops/
    rms/recreates containers and is about to certify the stack on the gate. A
    non-blocking probe of the ``/data`` update lock; held -> fail fast so the
    manager retries. No-op where there is no lock to probe (the probe returns
    False if fcntl or the lock file is unavailable)."""
    lock_path = Path(settings.greffon_path) / ".update.lock"
    if updater_spawn.update_in_progress(lock_path):
        raise HTTPException(status_code=409, detail="update_in_progress")


@router.post("/start/")
def start_greffon(
    payload: GreffonStartRequest, request: Request
) -> GreffonStartResponse:
    # Plain ``model_dump()``. ``configurations``/``ports`` have
    # ``default_factory`` on the model so an omitted key becomes an empty
    # container (not None and not absent), matching the strict vs safe
    # access patterns in apps/utils/greffon/repository.py
    # (``greffon['configurations']``) and apps/utils/docker/compose.py
    # (``.get('configurations', [])``). Explicit ``null`` is rejected by
    # Pydantic on type grounds.
    #
    # ``model_dump()`` also includes ``tunnel_client_toml`` — but the
    # downstream compose / repository code only reads keys it knows
    # about, so the extra field is harmless to pass through.
    _refuse_if_updating(_settings(request))
    greffon = payload.model_dump()
    compose_file = repository.get_compose_file_from_repository(greffon)
    # L4 (Tier-C) ports are published directly on their service. The bind
    # interface depends on this greffer's mode: proxy publishes on the public
    # interface; tunnel binds host-internal (reached by the rathole-client, not
    # the public interface). Resolve it BEFORE allocation so the sticky-port
    # free-probe and range allocation use the SAME interface the port is
    # published on (a port free on 0.0.0.0 isn't necessarily free on 127.0.0.1).
    l4_bind_host = (
        '127.0.0.1'
        if _settings(request).greffer_mode == 'tunnel'
        else '0.0.0.0'
    )
    # L4 host-port allocation can fail in three ways the operator must see as a
    # clean start error rather than an opaque 500 or a silent crash-looping
    # container: a proxy same_port endpoint whose pinned port a neighbour took
    # (409), an exhausted L4 range (409), or an unreachable docker daemon (503).
    try:
        greffon_info = repository.get_greffon_info(
            compose_file, greffon, l4_bind_host=l4_bind_host)
    except l4_ports.L4SamePortConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except l4_ports.L4PortRangeExhausted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except l4_ports.L4PortsUnavailable as exc:
        logger.warning("%s", exc)  # exc message already carries the machine code
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Tag this request's logs with the instance id (Feature #4), so the compose
    # run correlates with the manager action by both request_id and instance_id.
    instance_id_var.set(greffon_info["id"])
    # compose.py and _compute_instance_context both read the bind interface
    # off greffon_info. Set it BEFORE build_render_context so the instance_l4_*
    # tunnel/proxy branch is resolved against the real interface on the first
    # (setdefault-based, idempotent) render-context build. Set too late, a
    # tunnel greffer would be misread as proxy and bake host-internal
    # instance_l4_* values.
    greffon_info['l4_bind_host'] = l4_bind_host
    # Tunnel-mode L4 endpoint hand-off (Gap 2): the public endpoint a
    # self-configuring L4 app must advertise is RATHOLE_PUBLIC_HOST:tunnel_port,
    # which only the manager knows (it owns the relay's port allocation). When
    # the manager supplies it, feed it into the render context as instance_l4_*
    # so the app boots advertising the right endpoint; _compute_instance_context
    # leaves these empty in tunnel mode otherwise (the setdefault calls below
    # win because they run before build_render_context).
    if l4_bind_host == '127.0.0.1' and greffon.get('instance_l4_host') \
            and greffon.get('instance_l4_port'):
        l4_host = str(greffon['instance_l4_host'])
        l4_port = str(greffon['instance_l4_port'])
        greffon_info['instance_l4_host'] = l4_host
        greffon_info['instance_l4_port'] = l4_port
        greffon_info['instance_l4_endpoint'] = f'{l4_host}:{l4_port}'
        greffon_info['instance_l4_proto'] = greffon.get('instance_l4_proto') or 'tcp'
    # Build the Jinja render context (instance_*, integrations, config) ONCE,
    # before apply_configuration, so render-flagged baked files can reference
    # it. Mutates greffon_info in place (setdefault); create_compose's own
    # context calls are idempotent no-ops afterward.
    compose.build_render_context(greffon_info)
    compose_template = compose.get_compose_template(compose_file, greffon_info)
    try:
        compose.apply_configuration(greffon_info, compose_file)
    except compose.ConfigRenderError as exc:
        # A render-flagged baked file referenced a missing/typo'd variable.
        # Fail loudly with a clean 422 instead of an opaque 500. No half-started
        # instance: this runs before create_compose/start and before any volume
        # copy. Files written for earlier destinations in the same pass stay on
        # the greffon path unreferenced (nothing is copied into a volume) and are
        # overwritten on the next deploy attempt.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _t0 = time.monotonic()
    try:
        compose.create_compose(compose_template, greffon_info)
        conf.create_nginx_conf(greffon_info)
        compose.create_volumes_then_copy_files(greffon_info)
        compose.start(greffon_info)
    except Exception:
        diag("compose_op", level=logging.WARNING, op="start", outcome="error",
             duration_ms=round((time.monotonic() - _t0) * 1000))
        raise
    diag("compose_op", op="start", outcome="ok",
         duration_ms=round((time.monotonic() - _t0) * 1000))

    # v3 push race fix: compose.start uses subprocess.Popen and returns
    # before docker-compose has actually brought up the containers and
    # bound the user-facing port. Writing client.toml at this point
    # would let rathole-client's file-watcher pick up a config that
    # points at a not-yet-listening backend; rathole-client would
    # forward → connection refused → user sees a transient 502 until
    # rathole-client retries. Wait for compose to report 'running'
    # before writing. Bounded timeout — on slow/stuck images we write
    # anyway and rely on rathole-client's reconnect to bridge the gap.
    #
    # Only wait when there's actually a config to write. There are
    # three skip cases:
    #   1. ``payload.tunnel_client_toml is None`` — proxy-mode greffer
    #      or v2-manager-+-v3-greffer rollout combo. No tunnel-side
    #      race to guard against because no client.toml is being
    #      pushed. (Codex P1 on greffer#25.)
    #   2. ``settings.greffer_tunnel_client_config_path`` is empty —
    #      the documented "disabled" mode (see the setting's docstring
    #      in app/settings.py). Wait would still incur polling cost
    #      while the subsequent write is a no-op. (Codex P2 on
    #      greffer#25.)
    #   3. Both: degenerate case — same outcome.
    # Otherwise (tunnel mode + path enabled), the race exists and the
    # wait guards against rathole-client picking up the new config
    # before nginx has bound the user-facing port.
    settings = _settings(request)
    push_target = settings.greffer_tunnel_client_config_path
    if payload.tunnel_client_toml is not None and push_target:
        _wait_for_compose_running(greffon_info["id"])
    config_write_status = _write_pushed_client_toml(payload, request)

    return GreffonStartResponse(
        ports=greffon_info["ports"],
        config_write_status=config_write_status,
    )


@router.post("/stop/")
def stop_greffon(
    payload: GreffonStopRequest, request: Request
) -> GreffonStopResponse:
    _refuse_if_updating(_settings(request))
    greffon = payload.model_dump()
    instance_id_var.set(greffon.get("id"))  # tag logs (Feature #4)
    _t0 = time.monotonic()
    try:
        compose.stop(greffon)
    except Exception:
        diag("compose_op", level=logging.WARNING, op="stop", outcome="error",
             duration_ms=round((time.monotonic() - _t0) * 1000))
        raise
    diag("compose_op", op="stop", outcome="ok",
         duration_ms=round((time.monotonic() - _t0) * 1000))

    # v3 push: write the manager-rendered client.toml AFTER the
    # container is stopped. The dropped server.toml service on the
    # manager side is what actually severs traffic; the greffer-side
    # file write here just lets rathole-client close its idle
    # forwarding pair. A failure is non-fatal — the manager logs it
    # and the next start will re-push the latest content.
    config_write_status = _write_pushed_client_toml(payload, request)

    return GreffonStopResponse(config_write_status=config_write_status)


def _wait_for_compose_running(greffon_id: str) -> None:
    """Poll compose.get_status until containers are running, or timeout.

    Bounded by _COMPOSE_READY_TIMEOUT_SECONDS. Logs a warning on timeout
    and returns — the caller writes client.toml anyway and rathole-
    client's reconnect-on-failure handles the brief gap. Never raises:
    a transient docker-socket error during polling counts as "still
    starting" and the loop continues.
    """
    deadline = time.monotonic() + _COMPOSE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status = compose.get_status(greffon_id)
        except Exception as exc:  # docker socket issues, race with stop, etc.
            logger.debug(
                "compose_status_transient_error greffon_id=%s err=%s",
                greffon_id, exc,
            )
            time.sleep(_COMPOSE_READY_POLL_INTERVAL_SECONDS)
            continue
        if status.get("status") == "running":
            logger.info(
                "compose_ready greffon_id=%s elapsed=%.2fs",
                greffon_id,
                _COMPOSE_READY_TIMEOUT_SECONDS - max(0, deadline - time.monotonic()),
            )
            return
        time.sleep(_COMPOSE_READY_POLL_INTERVAL_SECONDS)
    logger.warning(
        "compose_not_ready_within_timeout greffon_id=%s timeout=%.1fs — "
        "writing client.toml anyway; rathole-client will reconnect "
        "once backend binds",
        greffon_id, _COMPOSE_READY_TIMEOUT_SECONDS,
    )


def _write_pushed_client_toml(
    payload: GreffonStartRequest | GreffonStopRequest,
    request: Request,
) -> str:
    """Shared helper used by both start and stop handlers.

    Returns ``"ok"`` or ``"failed"`` matching the response model's
    ``ConfigWriteStatus`` literal. Never raises — even an unexpected
    exception (other than the documented OSError chain) is mapped to
    ``"failed"`` so the start/stop response shape stays predictable.
    """
    settings = _settings(request)
    target = settings.greffer_tunnel_client_config_path
    try:
        wrote = maybe_write_client_toml(payload.tunnel_client_toml, target)
    except TunnelConfigWriteError:
        # Already logged inside the helper; surface to manager.
        return "failed"
    except Exception:  # pragma: no cover — paranoid wrap
        logger.exception("tunnel_client_toml_write_unexpected_error")
        return "failed"
    if wrote:
        logger.debug("tunnel_client_toml_pushed_for_id=%s",
                     getattr(payload, "id", "?"))
    return "ok"


@router.post("/backup/", status_code=202)
def backup_greffon(
    payload: GreffonBackupRequest, request: Request
) -> GreffonBackupResponse:
    """Cold backup: 202 + background thread (HLD section 4). The in-process
    per-instance lock 409s a concurrent op; the manager-supplied backup_id is
    echoed in the backup-result callback."""
    _refuse_if_updating(_settings(request))
    try:
        backup.spawn_backup(_settings(request), payload.id, payload.backup_id)
    except backup.BusyError:
        raise HTTPException(status_code=409, detail="instance_busy")
    return GreffonBackupResponse(backup_id=payload.backup_id)


@router.post("/restore/", status_code=202)
def restore_greffon(
    payload: GreffonRestoreRequest, request: Request
) -> GreffonRestoreResponse:
    """Restore-in-place: 202 + background thread. The greffer restores by
    restic_snapshot_id (it cannot map the manager UUID) and finalizes via the
    restore-result callback carrying the safety snapshot id."""
    _refuse_if_updating(_settings(request))
    try:
        backup.spawn_restore(
            _settings(request), payload.id, payload.restic_snapshot_id,
            payload.restore_id,
        )
    except backup.BusyError:
        raise HTTPException(status_code=409, detail="instance_busy")
    return GreffonRestoreResponse(restore_id=payload.restore_id)


@router.get("/restore-status/")
def get_restore_status(id: str, restore_id: str, request: Request) -> dict:
    """Durable restore outcome for the manager's reconciler -- a stuck RestoreRun
    is never blind-failed (its volumes may already be overwritten)."""
    return backup.restore_status(_settings(request), id, restore_id)


@router.post("/update/", status_code=202)
def remote_update(
    payload: RemoteUpdateRequest, request: Request
) -> RemoteUpdateResponse:
    """Spawn the detached v2 updater container (greffer self-update v2).

    Fail-closed gating, in order:
      * ``greffer_remote_update_enabled`` is the operator-sovereign switch. OFF
        (default) -> 403 at the SOURCE, so remote update stays off even if a
        manager is misconfigured to offer it. The flag is also advertised in the
        register payload so a correct manager never shows the button when off.
      * ``greffer_updater_image`` must be a configured, digest-pinned ref;
        unset -> 503, never a silent ``:latest`` pull of the one container that
        recreates the greffer.

    The handler does NOT verify provenance or recreate anything itself: it spawns
    the signed updater, which takes the ``/data`` lock and runs the full
    verify-then-pull -> recreate -> health-gate -> rollback flow, then returns 202
    with the spawned container id. The target tag is validated by the model and
    passed to the updater via ``GREFFER_UPDATER_TARGET_TAG`` (no shell)."""
    settings = _settings(request)
    if not settings.greffer_remote_update_enabled:
        raise HTTPException(
            status_code=403,
            detail="remote_update_disabled",
        )
    if not settings.greffer_updater_image:
        # Flag on but image unwired: operator misconfiguration. 503 (not 500)
        # so the manager reports a retry-after-config condition, not a bug.
        logger.error(
            "remote_update_enabled but greffer_updater_image is unset; refusing")
        raise HTTPException(
            status_code=503,
            detail="updater_image_not_configured",
        )
    if not updater_spawn.is_digest_pinned(settings.greffer_updater_image):
        # The most privileged container in the flow (docker.sock = host root)
        # must be pinned by digest so a registry-side tag move can't swap it.
        # Operator misconfiguration -> 503, fail-closed, nothing spawned.
        logger.error(
            "greffer_updater_image is not digest-pinned (%s); refusing",
            settings.greffer_updater_image)
        raise HTTPException(
            status_code=503,
            detail="updater_image_not_digest_pinned",
        )

    # Refuse if an update is already recreating the stack (HLD section 10): avoid
    # spawning a second updater that would only fail to take the /data lock.
    _refuse_if_updating(settings)

    instance_id_var.set(None)  # node-level op, not tied to a greffon instance
    try:
        updater_id = updater_spawn.spawn_updater(
            image=settings.greffer_updater_image,
            target_tag=payload.target_tag,
            greffer_id=settings.greffer_id,
            data_dest=str(settings.greffon_path),
        )
    except updater_spawn.UpdaterSpawnError as exc:
        logger.error("remote_update_spawn_failed target=%s err=%s",
                     payload.target_tag, exc)
        raise HTTPException(status_code=500, detail="updater_spawn_failed") from exc

    logger.info("remote_update_accepted target=%s updater=%s",
                payload.target_tag, updater_id[:12])
    return RemoteUpdateResponse(updater_id=updater_id)


@router.get("/greffon/{greffon_id}/")
def greffon_status(greffon_id: UUID) -> GreffonStatusResponse:
    # Legacy Django view calls ``compose.status`` which does not exist —
    # it's always thrown AttributeError in prod. The correct function is
    # ``get_status`` (the monitoring thread uses it correctly). Fixed here
    # as part of the port; see hld-api-parity.md § Latent bug.
    result = compose.get_status(str(greffon_id))
    return GreffonStatusResponse(**result)


@router.get("/greffon/{greffon_id}/stats/")
async def greffon_stats(
    greffon_id: UUID, request: Request
) -> InstanceStatsResponse:
    """One-shot digested per-container stats (resource-monitoring epic,
    Feature 2). The blocking Docker fan-out is offloaded to the threadpool
    under the dedicated metrics limiter so it never starves start/stop. A
    not-deployed instance is 404 ``missing_on_greffer``; a deployed-but-stopped
    instance is a 200 with null metrics. ``greffon_id: UUID`` rejects a crafted
    id before any handler body runs (the greffer maps the resulting
    RequestValidationError to 400, see app/errors.py).

    Id contract: the manager sends lowercase-canonical UUIDs (Django
    ``UUIDField``); ``str(greffon_id)`` emits that same canonical form, which
    matches the ``-p <id>`` project name ``compose.start`` pins. A
    non-canonical/uppercase id would normalise here and not match the
    enumeration label, but the manager never sends one."""
    body = await anyio.to_thread.run_sync(
        observe.cached_instance_stats, str(greffon_id),
        limiter=request.app.state.metrics_limiter,
    )
    if body is None:
        raise HTTPException(status_code=404, detail="missing_on_greffer")
    return InstanceStatsResponse(**body)


@router.get("/greffon/{greffon_id}/disk/")
async def greffon_disk(
    greffon_id: UUID, request: Request
) -> InstanceDiskResponse:
    """Lazy, TTL-cached per-instance disk usage (resource-monitoring epic,
    Feature 2): bind app-dir size plus the instance's volumes sliced from one
    shared host-wide ``df`` snapshot. Offloaded under the metrics limiter; a
    not-deployed instance is 404 ``missing_on_greffer``."""
    body = await anyio.to_thread.run_sync(
        observe.cached_instance_disk, str(greffon_id),
        limiter=request.app.state.metrics_limiter,
    )
    if body is None:
        raise HTTPException(status_code=404, detail="missing_on_greffer")
    return InstanceDiskResponse(**body)


@router.get("/greffon/{greffon_id}/logs/")
async def greffon_logs(
    greffon_id: UUID,
    request: Request,
    stream: Literal["container", "all", "deploy"] = "all",
    tail: int = instance_logs.LOG_TAIL_DEFAULT,
    since: str | None = None,
    service: str | None = None,
) -> InstanceLogsResponse:
    """Bounded per-instance log read (resource-monitoring epic, Feature 2, logs
    slice). ``stream`` selects container stdout/stderr (``container``/``all``)
    or the captured ``deploy`` log; ``service`` narrows a container read to one
    compose service (the per-container selector, ignored for ``deploy``);
    ``tail`` bounds the window (clamped to LOG_TAIL_MAX) and ``since`` is the
    opaque cursor for de-duplicating follow polls.

    Gated by ``GREFFER_LOG_SURFACING_ENABLED``: when off this endpoint 404s at
    the SOURCE, so logs stay off even if a manager is misconfigured. A
    not-deployed instance with no deploy log is 404 ``missing_on_greffer``; a
    malformed cursor is 400."""
    if not request.app.state.settings.greffer_log_surfacing_enabled:
        raise HTTPException(status_code=404, detail="log_surfacing_disabled")
    try:
        body = await anyio.to_thread.run_sync(
            instance_logs.instance_logs, str(greffon_id), stream, tail, since,
            service, limiter=request.app.state.metrics_limiter,
        )
    except instance_logs.BadCursor:
        raise HTTPException(status_code=400, detail="bad_cursor")
    if body is None:
        raise HTTPException(status_code=404, detail="missing_on_greffer")
    return InstanceLogsResponse(**body)


@router.post("/tunnel-config/")
def push_tunnel_config(
    payload: TunnelConfigPushRequest, request: Request
) -> TunnelConfigPushResponse:
    """v3 second-phase push of the rathole client.toml.

    Manager calls this AFTER ``/api/controller/start/`` or ``/stop/``
    has returned with port_host allocations, then renders client.toml
    against the post-allocation state and pushes the rendered file
    here. This split exists because manager doesn't know port_host
    until the greffer responds, and ``render_client_toml``'s
    ``local_addr`` lines depend on it.

    The bootstrap leg (initial client.toml after first accept) is
    delivered via the cert-poll response body instead — see
    ``app/workers/register.py``. Different shape because the greffer
    is the caller of that hop.

    Failure mode: if the file write fails (disk full, permission
    denied, etc.), we return ``config_write_status='failed'`` rather
    than raising. The manager surfaces it to the API caller. This
    keeps the start/stop end-to-end shape predictable — instance is
    up regardless of tunnel-config push outcome; operator sees the
    failed status and can retry by triggering another start/stop.

    Empty path setting (``settings.greffer_tunnel_client_config_path``)
    treats this as a no-op — useful in test environments. Returns
    ``ok`` rather than ``failed`` since no write was attempted.
    """
    settings = _settings(request)
    target = settings.greffer_tunnel_client_config_path
    if not target:
        logger.debug("tunnel_config_push: path empty, no-op")
        return TunnelConfigPushResponse(config_write_status="ok")

    try:
        write_client_toml(payload.client_toml, target)
    except TunnelConfigWriteError:
        # Already logged in the helper.
        return TunnelConfigPushResponse(config_write_status="failed")
    except Exception:  # pragma: no cover — paranoid wrap
        logger.exception("tunnel_config_push_unexpected_error")
        return TunnelConfigPushResponse(config_write_status="failed")

    logger.info(
        "tunnel_config_push_succeeded path=%s bytes=%d",
        target, len(payload.client_toml),
    )
    return TunnelConfigPushResponse(config_write_status="ok")
