"""Socket-only per-container recreate primitives for the v2 ``:latest`` updater.

No compose file: the updater talks to the Docker API (via the ``docker`` CLI,
mockable through ``greffer_cli.compose._run``) to update the running stack in
place. Per the HLD (``docs/features/greffer-self-update/
hld-v2-per-container-recreate.md``):

- discover the stack by compose **service label**, never the image name, which
  after a prior recreate shows as a bare digest with no ``greffon/<repo>`` tag
  (section 4);
- **verify-then-pull**: resolve ``:latest`` to its index digest ``D``,
  cosign-verify ``D`` bound to the repo, ``docker pull`` by ``@D`` (only the
  verified bytes), then retag local ``:latest`` to ``D`` so a later
  ``docker compose up`` cannot downgrade (sections 3 + 13, closes the tag-moved
  TOCTOU);
- tag the outgoing image ``:previous`` for a named rollback target (section 9);
- dangling-ONLY image prune on success (section 5).

Fail-closed: verification raises ``VerifyError`` before anything is recreated.
This module holds the Docker-API primitives only; ``engine`` orchestrates them
(recreate order, fidelity carry-over, the ``/readyz`` gate, rollback).
"""

from __future__ import annotations

import dataclasses
import json
import logging

from .. import compose, update
from . import provenance

logger = logging.getLogger("greffer-updater")

# Recreate order (HLD section 6): nginx first (a pure TLS proxy, safe to bounce);
# then greffer, whose recreate kills the FastAPI process that SPAWNED the updater
# (safe, the updater is a detached container and outlives its parent); then the
# tunnel-sidecar last.
RECREATE_ORDER: tuple[str, ...] = ("nginx", "greffer", "tunnel-sidecar")


class VerifyError(Exception):
    """A provenance check failed. Fail-closed: refuse before any recreate."""


@dataclasses.dataclass(frozen=True)
class StackContainer:
    """One container in the running greffon stack, keyed by its compose service
    label. ``repo`` is resolved from the service (``update.SERVICE_REPO``), NOT
    the image name, which may be a bare digest after a prior recreate."""

    service: str
    container_id: str
    repo: str


def _run(args: list[str], *, timeout: float = 60.0) -> compose.CommandResult:
    """Run ``docker <args>`` through the mockable subprocess layer."""
    return compose._run(["docker", *args], timeout=timeout)


def _greffer_container_id() -> str | None:
    """The greffer container id, found by its compose service label (section 4
    step 1). ``--all`` so a stopped greffer is still found."""
    res = _run(
        ["ps", "--all", "--filter", "label=com.docker.compose.service=greffer",
         "--format", "{{.ID}}"],
        timeout=30,
    )
    if not res.ok:
        return None
    ids = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return ids[0] if ids else None


def _project_of(container_id: str) -> str | None:
    """The ``com.docker.compose.project`` of a container (section 4 step 2)."""
    res = _run(
        ["inspect", "--format",
         '{{index .Config.Labels "com.docker.compose.project"}}', container_id],
        timeout=30,
    )
    if not res.ok:
        return None
    return res.stdout.strip() or None


def discover_stack() -> list[StackContainer]:
    """Discover the running greffon stack by compose labels (HLD section 4) and
    return the update set ordered per ``RECREATE_ORDER``. Empty list if the
    greffer container or its project cannot be found (the caller refuses).

    Selection is by the ``com.docker.compose.service`` label being one of
    ``update.SERVICE_REPO`` (greffer / nginx / tunnel-sidecar); the repo is
    mapped from that label, never read off the (possibly bare-digest) image."""
    greffer_id = _greffer_container_id()
    if not greffer_id:
        logger.error("stack discovery: no greffer container (compose service label)")
        return []
    project = _project_of(greffer_id)
    if not project:
        logger.error("stack discovery: greffer container has no compose project label")
        return []

    res = _run(
        ["ps", "--all",
         "--filter", f"label=com.docker.compose.project={project}",
         "--format", '{{.ID}}\t{{.Label "com.docker.compose.service"}}'],
        timeout=30,
    )
    if not res.ok:
        logger.error("stack discovery: listing project %s failed", project)
        return []

    found: dict[str, StackContainer] = {}
    for line in res.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2:
            continue
        container_id, service = parts[0].strip(), parts[1].strip()
        repo = update.SERVICE_REPO.get(service)
        if not container_id or not repo or service in found:
            continue
        found[service] = StackContainer(
            service=service, container_id=container_id, repo=repo)

    return [found[svc] for svc in RECREATE_ORDER if svc in found]


def current_image_id(repo: str) -> str | None:
    """The local image id ``<repo>:latest`` currently resolves to (the OUTGOING
    image). Captured BEFORE ``verify_then_pull`` moves the tag, so it can be
    named ``:previous`` and used as the in-flight rollback target (section 9)."""
    res = _run(["image", "inspect", "--format", "{{.Id}}", f"{repo}:latest"], timeout=30)
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def verify_then_pull(repo: str, *, cosign_pub: str) -> str:
    """Verify-then-pull for one repo (HLD sections 3 + 13). Resolve ``:latest``
    to its index digest ``D``, cosign-verify ``D`` bound to ``repo``, ``docker
    pull`` by ``@D`` (so only the verified bytes are ever fetched), then retag
    local ``:latest`` to ``D``. Returns ``D``. Raises ``VerifyError``
    fail-closed; recreates nothing.

    Pulling by digest then retagging (not pulling by tag) is what closes the
    tag-moved TOCTOU AND satisfies section 3's no-downgrade invariant (local
    ``:latest`` ends up on the verified digest)."""
    ref = f"{repo}:latest"
    digest = provenance.resolve_digest(ref)
    if not digest:
        raise VerifyError(f"cannot resolve digest for {ref}")
    if not provenance.cosign_verify(repo, digest, pubkey=cosign_pub):
        raise VerifyError(f"signature/repo-binding failed for {repo}@{digest}")
    by_digest = f"{repo}@{digest}"
    if not _run(["pull", by_digest], timeout=600).ok:
        raise VerifyError(f"pull failed for {by_digest}")
    # Refresh local :latest -> D. WITHOUT this, :latest stays on the old image
    # and the next `docker compose up` downgrades (section 3 anti-pattern).
    if not _run(["tag", by_digest, ref], timeout=30).ok:
        raise VerifyError(f"retag {by_digest} -> {ref} failed")
    return digest


def tag_previous(repo: str, image_id: str) -> bool:
    """Tag an outgoing image id ``<repo>:previous`` (the named, persistent
    rollback target, HLD section 9). Best-effort; a failure does not abort the
    update (the in-flight rollback still has the captured image id)."""
    if not image_id:
        return False
    ok = _run(["tag", image_id, f"{repo}:previous"], timeout=30).ok
    if not ok:
        logger.warning("could not tag %s as %s:previous", image_id, repo)
    return ok


def dangling_prune() -> None:
    """Dangling-ONLY ``docker image prune`` after a passed gate (HLD section 5).

    NEVER ``-a`` and NEVER an id-targeted ``docker rmi``: retagging ``:previous``
    leaves the image it displaced (N-2) untagged, i.e. dangling, which a plain
    prune reaps; a tagged or in-use image is never dangling, so this structurally
    cannot touch ``:latest`` / ``:previous`` or any running container's image
    (the collision after a durable rollback where ``:latest`` == ``:previous``
    is harmless here for the same reason). Best-effort."""
    res = _run(["image", "prune", "-f"], timeout=120)
    if not res.ok:
        logger.warning("dangling image prune failed (non-fatal): %s", res.stderr.strip())


# --- fidelity recreate (HLD section 8) ------------------------------
#
# Recreating from `docker inspect` means we own carrying the config across. The
# rule for image-influenced fields (Env, Labels, Cmd, Entrypoint, Healthcheck,
# User, WorkingDir, StopSignal) is the vs-OLD delta: carry a value only if it
# DIFFERS from the OLD image's baked config (= what compose set on top), then
# apply it on the new image via `docker create`, which inherits the new image's
# own defaults. Diffing against the NEW image would re-pin a baked default the
# new image deliberately changed (a relocated venv, or the new GREFFER_UPDATER_
# IMAGE digest), freezing it. Infra fields (Mounts, PortBindings, RestartPolicy,
# LogConfig, ExtraHosts, NetworkMode) are carried verbatim.


def inspect_container(container_id: str) -> dict | None:
    """Full ``docker inspect`` of a container as a dict, or None."""
    res = _run(["inspect", container_id], timeout=30)
    if not res.ok:
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def image_config(ref_or_id: str) -> dict:
    """The ``.Config`` of an image (the vs-OLD delta baseline). Empty dict if
    unreadable; the delta then diffs against ``{}`` and carries the container's
    config verbatim, the safe fail-open for fidelity (over-carry beats dropping
    runtime config like ``GREFFER_ID``). The old image is still present at
    recreate time (we prune only after the gate), so this normally succeeds."""
    res = _run(["image", "inspect", ref_or_id, "--format", "{{json .Config}}"], timeout=30)
    if not res.ok:
        return {}
    try:
        cfg = json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _kv_list_to_dict(env_list) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in env_list or []:
        if isinstance(item, str):
            k, _, v = item.partition("=")
            out[k] = v
    return out


def _env_delta(container_env, old_image_env) -> dict[str, str]:
    """vs-OLD env delta (HLD section 8): the entries the container has that the
    OLD image did not bake identically (the compose overlay)."""
    base = _kv_list_to_dict(old_image_env)
    return {k: v for k, v in _kv_list_to_dict(container_env).items() if base.get(k) != v}


def _dict_delta(container_map, old_image_map) -> dict[str, str]:
    """vs-OLD delta for a label-like dict: carry only compose-set/changed keys
    (so the ``com.docker.compose.*`` identity labels, absent from the image, are
    carried and the stack stays a recognizable compose project)."""
    base = old_image_map or {}
    return {k: v for k, v in (container_map or {}).items() if base.get(k) != v}


def _duration(ns) -> str:
    """Nanoseconds (docker inspect) -> a ``docker create`` duration string."""
    try:
        seconds = int(ns) / 1_000_000_000
    except (TypeError, ValueError):
        return "0s"
    return f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds}s"


def _network_args(host: dict, service: str) -> list[str]:
    """NetworkMode handling, the HLD section 8 "sidecar trap": host-networked
    sidecar gets ``--network host`` and no aliases; greffer/nginx rejoin their
    user-defined network with the service name as alias so greffer<->nginx DNS
    survives the recreate. A default bridge needs nothing."""
    mode = host.get("NetworkMode") or ""
    if mode == "host":
        return ["--network", "host"]
    if mode == "none":
        return ["--network", "none"]
    if mode in ("", "default", "bridge"):
        return []
    args = ["--network", mode]
    if service:
        args += ["--network-alias", service]
    return args


def _restart_args(host: dict) -> list[str]:
    rp = host.get("RestartPolicy") or {}
    name = rp.get("Name") or ""
    if not name or name == "no":
        return []
    if name == "on-failure":
        n = rp.get("MaximumRetryCount") or 0
        return ["--restart", f"on-failure:{n}"] if n else ["--restart", "on-failure"]
    return ["--restart", name]


def _log_args(host: dict) -> list[str]:
    lc = host.get("LogConfig") or {}
    driver = lc.get("Type") or ""
    if not driver:
        return []
    args = ["--log-driver", driver]
    for k, v in (lc.get("Config") or {}).items():
        args += ["--log-opt", f"{k}={v}"]
    return args


def _mount_args(mounts) -> list[str]:
    """Reconstruct ``-v`` from ``.Mounts`` (NOT raw ``HostConfig.Binds``): the
    sidecar's ``:ro`` (``RW: false``) is load-bearing and easy to drop."""
    args: list[str] = []
    for m in mounts or []:
        dst = m.get("Destination")
        mtype = m.get("Type")
        src = m.get("Name") if mtype == "volume" else m.get("Source") if mtype == "bind" else None
        if not dst or not src:
            if dst and mtype not in ("volume", "bind"):
                logger.warning("skipping unsupported mount type %r at %s", mtype, dst)
            continue
        spec = f"{src}:{dst}"
        if m.get("RW") is False:
            spec += ":ro"
        args += ["-v", spec]
    return args


def _port_args(port_bindings) -> list[str]:
    args: list[str] = []
    for container_port, binds in (port_bindings or {}).items():
        cp, _, proto = str(container_port).partition("/")
        for b in binds or []:
            host_ip, host_port = b.get("HostIp") or "", b.get("HostPort") or ""
            if host_ip:
                spec = f"{host_ip}:{host_port}:{cp}"
            elif host_port:
                spec = f"{host_port}:{cp}"
            else:
                spec = cp
            if proto and proto != "tcp":
                spec += f"/{proto}"
            args += ["-p", spec]
    return args


def _healthcheck_args(cont_hc, old_img_hc) -> list[str]:
    """vs-OLD healthcheck delta: carry only if compose set/changed it (the
    sidecar's ``pgrep rathole`` is compose-set; greffer/nginx have none)."""
    cont_hc = cont_hc or {}
    if cont_hc == (old_img_hc or {}):
        return []
    test = cont_hc.get("Test") or []
    if not test or test == ["NONE"]:
        return ["--no-healthcheck"] if old_img_hc else []
    kind = test[0]
    if kind == "CMD-SHELL" and len(test) >= 2:
        args = ["--health-cmd", test[1]]
    elif kind == "CMD":
        args = ["--health-cmd", " ".join(test[1:])]
    else:
        args = ["--health-cmd", " ".join(test)]
    for flag, key in (("--health-interval", "Interval"), ("--health-timeout", "Timeout"),
                      ("--health-start-period", "StartPeriod")):
        if cont_hc.get(key):
            args += [flag, _duration(cont_hc[key])]
    if cont_hc.get("Retries"):
        args += ["--health-retries", str(cont_hc["Retries"])]
    return args


_SCALAR_FIELDS = (("User", "--user"), ("WorkingDir", "--workdir"), ("StopSignal", "--stop-signal"))


def _scalar_delta_args(cont_cfg: dict, old_img_cfg: dict) -> list[str]:
    args: list[str] = []
    for key, flag in _SCALAR_FIELDS:
        cval = cont_cfg.get(key) or ""
        if cval and cval != ((old_img_cfg or {}).get(key) or ""):
            args += [flag, cval]
    return args


def _entrypoint_override(cont_ep, old_img_ep) -> str | None:
    cont_ep = cont_ep or []
    if cont_ep == (old_img_ep or []) or not cont_ep:
        return None
    if len(cont_ep) > 1:
        logger.warning(
            "multi-element entrypoint override not fully expressible via "
            "docker create --entrypoint: %r; carrying argv[0] only", cont_ep)
    return cont_ep[0]


def _cmd_override(cont_cmd, old_img_cmd) -> list[str]:
    cont_cmd = cont_cmd or []
    return [] if cont_cmd == (old_img_cmd or []) else list(cont_cmd)


def build_create_argv(container: dict, old_image_config: dict, *,
                      image_ref: str, service: str) -> list[str]:
    """Build the ``docker create`` argv that recreates ``container`` from
    ``image_ref``, carrying the compose-set config per the HLD section 8
    fidelity rule (image-influenced fields = vs-OLD delta against
    ``old_image_config``; infra fields = verbatim). Pure: no docker calls."""
    cfg = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    old_cfg = old_image_config or {}
    name = (container.get("Name") or "").lstrip("/")

    argv: list[str] = ["create", "--name", name]
    argv += _network_args(host, service)
    argv += _restart_args(host)
    argv += _log_args(host)
    for h in host.get("ExtraHosts") or []:
        argv += ["--add-host", h]
    for k, v in _env_delta(cfg.get("Env"), old_cfg.get("Env")).items():
        argv += ["-e", f"{k}={v}"]
    for k, v in _dict_delta(cfg.get("Labels"), old_cfg.get("Labels")).items():
        argv += ["--label", f"{k}={v}"]
    argv += _mount_args(container.get("Mounts"))
    argv += _port_args(host.get("PortBindings"))
    argv += _healthcheck_args(cfg.get("Healthcheck"), old_cfg.get("Healthcheck"))
    argv += _scalar_delta_args(cfg, old_cfg)
    entrypoint = _entrypoint_override(cfg.get("Entrypoint"), old_cfg.get("Entrypoint"))
    if entrypoint is not None:
        argv += ["--entrypoint", entrypoint]
    argv.append(image_ref)
    argv += _cmd_override(cfg.get("Cmd"), old_cfg.get("Cmd"))
    return argv


def recreate_one(container: StackContainer, *, image_ref: str,
                 old_image_id: str | None, stop_timeout: float = 30.0) -> bool:
    """Recreate one stack container from ``image_ref``, carrying its config per
    section 8. inspect -> stop -> rm -> create (same name) -> start. Returns
    True on success. ``old_image_id`` (captured before the pull moved
    ``:latest``) supplies the vs-OLD delta baseline and is still present."""
    inspected = inspect_container(container.container_id)
    if not inspected:
        logger.error("recreate %s: cannot inspect %s", container.service, container.container_id)
        return False
    old_cfg = image_config(old_image_id) if old_image_id else {}
    if not old_cfg:
        logger.warning(
            "recreate %s: old image config unreadable, carrying container config "
            "verbatim (may pin a baked default the new image changed)", container.service)
    argv = build_create_argv(
        inspected, old_cfg, image_ref=image_ref, service=container.service)
    name = (inspected.get("Name") or "").lstrip("/")
    if not name:
        logger.error("recreate %s: container has no name", container.service)
        return False

    _run(["stop", "-t", str(int(stop_timeout)), container.container_id],
         timeout=stop_timeout + 10)
    if not _run(["rm", "-f", container.container_id], timeout=60).ok:
        logger.error("recreate %s: removing old container failed", container.service)
        return False
    if not _run(argv, timeout=120).ok:
        logger.error("recreate %s: docker create failed", container.service)
        return False
    if not _run(["start", name], timeout=60).ok:
        logger.error("recreate %s: docker start failed", container.service)
        return False
    logger.info("recreated %s from %s", container.service, image_ref)
    return True
