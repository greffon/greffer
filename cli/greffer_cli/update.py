"""``greffer update`` — update a running greffer node to a newer image.

Five-step engine (design: greffon docs/features/greffer-self-update,
merged greffon#165):

  1. resolve the target tag (--to > manifest latest)
  2. pre-flight (persistent /data volume, rollback-safety gate)
  3. rewrite every greffon/* image line -> target, pull, recreate
  4. health-gate (/healthz live -> /readyz ready + id + version applied
     + all active services running; restart-count fail-fast)
  5. roll back to the prior refs on any failure (the authority)

Mirrors up.py's structure. The compose layer (compose.py) is called
directly so tests monkeypatch it; ``sleep`` is injected so the polling
loop is testable without real waits. No Docker is needed for the unit
tests.
"""

from __future__ import annotations

import dataclasses
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import compose, env_file, paths, strings

# The published node images this engine retags, pulls, and recreates
# together. greffer-cli pins all three to the same <TAG> in the rendered
# compose, so a node update moves the whole node.
GREFFER_REPO = "greffon/greffer"
NGINX_REPO = "greffon/greffer-nginx"
SIDECAR_REPO = "greffon/tunnel-sidecar"
SERVICE_REPO = {
    "greffer": GREFFER_REPO,
    "nginx": NGINX_REPO,
    "tunnel-sidecar": SIDECAR_REPO,
}

DEFAULT_MANIFEST_URL = "https://greffon.io/greffer-version.json"

# Exit codes (subcommand-specific; see HLD § "v1 CLI surface").
EXIT_OK = 0                      # success or no-op
EXIT_FAILED_ROLLED_BACK = 1      # update failed, rolled back cleanly
EXIT_FAILED_ROLLBACK_FAILED = 2  # update failed AND rollback failed
EXIT_PREFLIGHT_REFUSED = 3       # refused before any pull

# Health-gate outcomes.
GATE_READY = "ready"
GATE_TIMEOUT = "timeout"
GATE_FATAL = "fatal"
GATE_WRONG_ID = "wrong_id"
GATE_NOT_APPLIED = "not_applied"
GATE_DEGRADED_OTHER = "degraded_other"
GATE_CRASH_LOOP = "crash_loop"

# Degraded /readyz reasons the gate waits through rather than failing on
# (the greffer is mid-re-registration, which a recreate always triggers).
# A set so the gate stays correct if the app ever reports several reasons
# at once; any reason outside this set fails the gate.
TOLERABLE_DEGRADED_REASONS = {"registration_pending"}


# --- Version manifest ------------------------------------------------

@dataclasses.dataclass
class Manifest:
    latest: str | None = None
    min_supported: str | None = None
    no_rollback_from: list[str] = dataclasses.field(default_factory=list)


# A tiny JSON manifest; cap the read so a hostile endpoint can't stream an
# unbounded body into the operator's process.
_MAX_MANIFEST_BYTES = 256 * 1024


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would downgrade the manifest fetch to plaintext.

    The default handler follows an https->http 302 transparently, which would
    defeat the first-hop https guard in fetch_manifest. Re-check the scheme on
    every hop and raise an HTTPError (a URLError subclass, so fetch_manifest's
    ``except`` turns it into a fail-closed ``None``) on any non-https target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code, "refusing redirect off https for the manifest",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_MANIFEST_OPENER = urllib.request.build_opener(_HttpsOnlyRedirectHandler)


def fetch_manifest(
    url: str = DEFAULT_MANIFEST_URL, *, timeout: float = 10.0,
) -> Manifest | None:
    """GET the version manifest over HTTPS. Returns ``None`` if unreachable
    or malformed — the caller treats that as "unknown", not a hard error."""
    if not url.startswith("https://"):
        # The manifest carries update targets; never fetch it over plaintext.
        return None
    try:
        # _MANIFEST_OPENER re-checks the scheme on every redirect hop, so an
        # https->http downgrade is refused rather than silently followed. The
        # read is bounded (see _MAX_MANIFEST_BYTES).
        with _MANIFEST_OPENER.open(url, timeout=timeout) as r:
            raw = r.read(_MAX_MANIFEST_BYTES + 1)
        body = raw.decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(raw) > _MAX_MANIFEST_BYTES:
        return None  # oversized -> treat as malformed/unreachable (fail-closed)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    nrf = data.get("no_rollback_from") or []
    if not isinstance(nrf, list):
        nrf = []
    # Coerce non-string version fields to None: a tampered/malformed manifest
    # could carry a list or object here, which must never flow into an image
    # ref. Tag *grammar* is enforced later (resolve_target / set_image_tag).
    latest = data.get("latest")
    if not isinstance(latest, str):
        latest = None
    min_supported = data.get("min_supported")
    if not isinstance(min_supported, str):
        min_supported = None
    return Manifest(
        latest=latest,
        min_supported=min_supported,
        no_rollback_from=[str(x) for x in nrf],
    )


def resolve_target(
    *, explicit_to: str | None, manifest: Manifest | None,
) -> str | None:
    """Resolve the target tag. ``--to`` wins; else the manifest's latest;
    else ``None`` (auto-latest with an unreachable manifest -> abort).

    A syntactically invalid tag (from a tampered manifest or a bad ``--to``)
    is rejected here (-> ``None``) so it can never reach the compose file;
    the CLI boundary (`main.update`) also rejects an invalid ``--to`` with a
    precise message before we get here."""
    if explicit_to:
        return explicit_to if compose.is_valid_image_tag(explicit_to) else None
    if manifest and manifest.latest and compose.is_valid_image_tag(manifest.latest):
        return manifest.latest
    return None


def no_rollback_blocked(
    manifest: Manifest | None, current: str | None, target: str,
) -> bool | None:
    """Is current->target flagged "no in-place rollback"?

    ``True``  = the pair is listed (blocked; needs --confirm-no-rollback).
    ``False`` = manifest reachable and the pair is absent => rollback-safe.
    ``None``  = unknown (manifest unreachable, or current version unknown).
    """
    if manifest is None or current is None:
        return None
    return f"{current}->{target}" in manifest.no_rollback_from


# --- /readyz parsing -------------------------------------------------

@dataclasses.dataclass
class Readiness:
    ok: bool                 # got an HTTP response with a JSON body
    id: str | None
    status: str | None       # "ready" | "degraded" | "fatal"
    reasons: list[str]


def parse_readyz(result: compose.CommandResult) -> Readiness:
    if not result.ok:
        return Readiness(ok=False, id=None, status=None, reasons=[])
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return Readiness(ok=False, id=None, status=None, reasons=[])
    if not isinstance(data, dict):
        return Readiness(ok=False, id=None, status=None, reasons=[])
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = []
    return Readiness(
        ok=True, id=data.get("id"), status=data.get("status"),
        reasons=[str(r) for r in reasons],
    )


def active_services(mode: str) -> list[str]:
    """Control-plane services the rendered compose runs for the mode.

    nginx runs in both modes; the tunnel-sidecar only under --profile tunnel.
    """
    svcs = ["greffer", "nginx"]
    if mode == "tunnel":
        svcs.append("tunnel-sidecar")
    return svcs


def profile_for_mode(mode: str) -> str | None:
    return "tunnel" if mode == "tunnel" else None


def _resolve_mode(env: env_file.EnvFile, compose_file: Path) -> str:
    """Deployment mode for the running node.

    Prefer the persisted ``GREFFER_MODE`` (``greffer up`` always writes it).
    If it's absent (a hand-edited or pre-``GREFFER_MODE`` env.env), detect
    it from the running node rather than guessing a static default: the
    tunnel-sidecar only has a container under ``--profile tunnel``, so its
    presence means tunnel mode. Falls back to proxy (the smaller service
    set) when nothing is running to inspect, an edge that ``greffer up``
    (which always stamps the field) prevents in practice.
    """
    persisted = env.get("GREFFER_MODE")
    if persisted:
        return persisted
    running = compose.compose_services_running(compose_file, profile="tunnel")
    return "tunnel" if "tunnel-sidecar" in running else "proxy"


def _version_applied(
    compose_file: Path, target: str, *, applied_ref: str | None = None
) -> bool:
    """Did the recreate actually advance the greffer to the intended image?

    Compares the running greffer container's image id to the intended image id.
    ``applied_ref`` overrides the lookup ref when the recreate pinned a DIGEST
    rather than a tag (the v2 remote-update path: it pulls/pins
    ``greffon/greffer@sha256:...`` and never creates the local
    ``greffon/greffer:<target>`` tag, so a tag lookup would spuriously read None
    and fail the gate). When unset (the v1 tag path) it falls back to the
    ``greffon/greffer:<target>`` tag. A mispublished/moved tag or an unbumped
    image leaves the old image running and fails this.
    """
    running = compose.container_image_id(compose_file, "greffer")
    target_id = compose.image_id(applied_ref or f"{GREFFER_REPO}:{target}")
    return bool(running and target_id and running == target_id)


def health_gate(
    compose_file: Path, *, greffer_id: str | None, target: str,
    services: list[str], profile: str | None,
    timeout: float, poll_interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    applied_ref: str | None = None,
) -> str:
    """Poll until the recreated node is healthy or a failure is decided.

    Success (``GATE_READY``) requires all of: ``/healthz`` live, ``/readyz``
    ``ready`` with matching ``id``, the greffer version actually applied,
    and every active service running. ``degraded: registration_pending`` is
    awaited (the greffer is re-registering). A non-matching id, a ``fatal``,
    any other ``degraded`` reason, a crash-loop, or the timeout fail.
    """
    deadline = time.monotonic() + timeout
    cid = compose.service_container_id(compose_file, "greffer")
    base_restarts = compose.docker_inspect_restart_count(cid) if cid else 0
    while time.monotonic() < deadline:
        if compose.exec_in_greffer_healthz(compose_file).ok:
            r = parse_readyz(compose.exec_in_greffer_readyz(compose_file))
            if r.ok:
                if greffer_id and r.id and r.id != greffer_id:
                    return GATE_WRONG_ID
                if r.status == "fatal":
                    return GATE_FATAL
                if r.status == "degraded" and any(
                    reason not in TOLERABLE_DEGRADED_REASONS for reason in r.reasons
                ):
                    return GATE_DEGRADED_OTHER
                if r.status == "ready":
                    if not _version_applied(
                        compose_file, target, applied_ref=applied_ref
                    ):
                        return GATE_NOT_APPLIED
                    svcs = compose.compose_services_running(
                        compose_file, profile=profile,
                    )
                    if svcs and all(svcs.get(s, False) for s in services):
                        return GATE_READY
                    # greffer ready but a retagged sibling (nginx / sidecar)
                    # is not up yet; keep polling, the timeout is the backstop.
        # Supplementary fail-fast: a climbing restart count past the
        # post-recreate baseline means the new image is crash-looping.
        if cid and compose.docker_inspect_restart_count(cid) - base_restarts > 1:
            return GATE_CRASH_LOOP
        sleep(poll_interval)
    return GATE_TIMEOUT


def _rollback(
    compose_file: Path, *, profile: str | None, services: list[str],
    old_refs: dict[str, str], greffer_id: str | None,
    timeout: float, sleep: Callable[[float], None],
) -> int:
    """Restore the prior image refs and recreate. The prior images are still
    cached locally (we only pulled the target tag, never re-pulled the prior),
    so restoring the refs + ``up`` (no pull) brings the exact prior node back.

    Returns ``EXIT_FAILED_ROLLED_BACK`` if the rolled-back node is healthy,
    else ``EXIT_FAILED_ROLLBACK_FAILED`` (no loop, manual recovery)."""
    compose.set_image_refs(compose_file, old_refs)
    up = compose.compose_up(compose_file, profile=profile)
    if up.ok and _rollback_health(
        compose_file, services=services, profile=profile,
        greffer_id=greffer_id, timeout=min(timeout, 120.0), sleep=sleep,
    ):
        print(strings.UPDATE_ROLLED_BACK, file=sys.stderr)
        return EXIT_FAILED_ROLLED_BACK
    print(strings.UPDATE_ROLLBACK_FAILED.format(compose_path=compose_file), file=sys.stderr)
    return EXIT_FAILED_ROLLBACK_FAILED


def _rollback_health(
    compose_file: Path, *, services: list[str], profile: str | None,
    greffer_id: str | None, timeout: float,
    sleep: Callable[[float], None],
) -> bool:
    """Confirm the rolled-back node reaches /readyz ready + all services up.
    No version check (rollback restores the prior version, not the target)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if compose.exec_in_greffer_healthz(compose_file).ok:
            r = parse_readyz(compose.exec_in_greffer_readyz(compose_file))
            if r.ok and r.status == "ready" and not (
                greffer_id and r.id and r.id != greffer_id
            ):
                svcs = compose.compose_services_running(compose_file, profile=profile)
                if svcs and all(svcs.get(s, False) for s in services):
                    return True
        sleep(2.0)
    return False


# --- concurrency lock + interrupt disarm -----------------------------

# Sentinel: platform without fcntl (Windows) -> proceed without a host lock.
_NO_HOST_LOCK = object()


def _update_lock_path(cfg: Path) -> Path:
    """The host update lock. Prefer the ``/data`` volume mountpoint so a host
    ``greffer update`` and the in-container remote updater flock the SAME inode
    (HLD section 10: the updater locks ``/data/.update.lock``, which IS this
    mountpoint inside its container). Fall back to the config dir if the volume
    can't be resolved (degraded: still serializes two host runs, just not
    host-vs-remote). Filename ``.update.lock`` matches on both sides."""
    mountpoint = compose.data_volume_mountpoint(paths.docker_compose_yml_path(cfg))
    return (Path(mountpoint) if mountpoint else cfg) / ".update.lock"


def _acquire_update_lock(cfg: Path):
    """Take an exclusive host lock so two concurrent ``greffer update`` runs
    can't interleave the compose rewrite and rollback-baseline capture (run B
    reading the file after run A already retagged it would record A's
    unvalidated target as the "prior" ref).

    Returns an open handle to release later, ``None`` if another run already
    holds the lock, or ``_NO_HOST_LOCK`` on a platform without ``fcntl``
    (Windows) where we proceed unlocked; concurrent runs there are the
    operator's responsibility (v1 is operator-run)."""
    try:
        import fcntl
    except ImportError:
        return _NO_HOST_LOCK
    try:
        fh = open(_update_lock_path(cfg), "w", encoding="utf-8")
    except OSError:
        # The resolved /data volume mountpoint isn't present/writable (e.g. the
        # volume was never created). Fall back to the config dir so a host update
        # still serializes against another host run, rather than crashing
        # (degraded: it won't contend with the in-container remote updater).
        fh = open(cfg / ".update.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _release_update_lock(handle) -> None:
    if handle is None or handle is _NO_HOST_LOCK:
        return
    try:
        handle.close()  # closing the fd releases the flock
    except OSError:
        pass


def _install_sigterm_handler(on_signal: Callable[[], None]):
    """Route a SIGTERM during the mutating window through ``on_signal`` (which
    disarms and exits with the right code), so a kill mid-update doesn't leave
    the compose file pinned to an un-gated target. Ctrl-C (KeyboardInterrupt)
    and exceptions are already covered by the surrounding ``finally``; this adds
    the bare-SIGTERM case. Returns the prior handler to restore, or ``None`` if
    one can't be installed (non-main thread / unsupported platform)."""
    def _handler(_signum, _frame):
        on_signal()
    try:
        return signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError, AttributeError):
        return None


def _restore_sigterm(prev) -> None:
    if prev is None:
        return
    try:
        signal.signal(signal.SIGTERM, prev)
    except (ValueError, OSError, AttributeError):
        pass


def run_update(
    cfg: Path, *,
    target: str | None = None,
    manifest_url: str = DEFAULT_MANIFEST_URL,
    timeout: float = 600.0,
    confirm_no_rollback: bool = False,
    check_only: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Drive the update. Returns one of the EXIT_* codes."""
    compose_file = paths.docker_compose_yml_path(cfg)
    env = env_file.EnvFile.read(paths.env_env_path(cfg))
    greffer_id = env.get("GREFFER_ID")
    mode = _resolve_mode(env, compose_file)
    profile = profile_for_mode(mode)
    services = active_services(mode)

    # ---- 1. Resolve target -------------------------------------------
    manifest = fetch_manifest(manifest_url)
    resolved = resolve_target(explicit_to=target, manifest=manifest)
    current_version = compose.exec_greffer_version(compose_file)
    if resolved is None:
        if manifest is not None and target is None:
            # Manifest reached but its `latest` is missing / non-string /
            # not a valid tag (a server-side problem, not connectivity).
            print(strings.UPDATE_BAD_MANIFEST, file=sys.stderr)
        else:
            print(strings.UPDATE_NO_TARGET, file=sys.stderr)
        return EXIT_PREFLIGHT_REFUSED

    if check_only:
        behind = (
            current_version is not None
            and resolved != current_version
        )
        print(strings.UPDATE_CHECK.format(
            current=current_version or "unknown", target=resolved,
            available=("yes" if behind else "no/unknown"),
        ))
        return EXIT_OK

    # ---- 2. Pre-flight (abort before any pull) -----------------------
    if not greffer_id:
        # The post-recreate gate confirms the node still answers as this
        # greffer; with no known id it can't, so refuse rather than update
        # blind. `greffer up` always stamps GREFFER_ID, so a missing one
        # means a hand-corrupted env.env.
        print(strings.UPDATE_PREFLIGHT_NO_ID, file=sys.stderr)
        return EXIT_PREFLIGHT_REFUSED
    if not compose.data_volume_is_named(compose_file):
        print(strings.UPDATE_PREFLIGHT_NO_DATA_VOLUME, file=sys.stderr)
        return EXIT_PREFLIGHT_REFUSED

    blocked = no_rollback_blocked(manifest, current_version, resolved)
    if blocked is not False and not confirm_no_rollback:
        # listed (True) or unknown (None) -> require explicit confirmation
        print(strings.UPDATE_NEEDS_CONFIRM_NO_ROLLBACK.format(
            current=current_version or "unknown", target=resolved,
        ), file=sys.stderr)
        return EXIT_PREFLIGHT_REFUSED

    # Idempotency: already running the target image? (digest compare, so it
    # holds even if a prior rollback left the compose digest-pinned.)
    running_id = compose.container_image_id(compose_file, "greffer")
    target_id = compose.image_id(f"{GREFFER_REPO}:{resolved}")
    if running_id and target_id and running_id == target_id:
        print(strings.UPDATE_ALREADY.format(target=resolved))
        return EXIT_OK

    # Serialize the mutating path on a host lock so two concurrent updates
    # can't race the compose rewrite / rollback-baseline capture.
    lock = _acquire_update_lock(cfg)
    if lock is None:
        print(strings.UPDATE_IN_PROGRESS.format(lock_path=_update_lock_path(cfg)),
              file=sys.stderr)
        return EXIT_PREFLIGHT_REFUSED
    try:
        # ---- 3. Rewrite + pull + recreate ----------------------------
        old_refs = compose.set_image_tag(compose_file, resolved)
        gate_passed = False

        def _disarm() -> None:
            # Backstop for an interrupt/crash between the retag and a passed
            # gate: restore the prior refs so a later bare `docker compose up`
            # (operator, deploy script, host reboot) can't recreate the node
            # into the un-gated target. The failure branches below reach this
            # via the same flag; it also covers Ctrl-C / SIGTERM mid-pull or
            # mid-gate, which would otherwise skip rollback entirely.
            if not gate_passed:
                compose.set_image_refs(compose_file, old_refs)

        def _on_sigterm() -> None:
            # A kill mid-update: disarm, then exit. Once the gate has passed the
            # update is already applied and the file is correct, so exit success
            # (0), not 143, so a wrapper keying on the exit code does not read a
            # confirmed success as a failure during the brief window before the
            # handler is restored.
            _disarm()
            raise SystemExit(EXIT_OK if gate_passed else 143)

        prev_sigterm = _install_sigterm_handler(_on_sigterm)
        try:
            # Pull (and thereby validate) ALL node images, including the
            # profile-gated tunnel-sidecar in proxy mode: set_image_tag
            # retagged its line too, so a bad target tag must be caught here
            # rather than left to break a later proxy->tunnel switch. Recreate
            # and the health gate stay mode-scoped below.
            pull = compose.compose_pull(
                compose_file, profile="tunnel", services=list(SERVICE_REPO),
            )
            if not pull.ok:
                print(strings.UPDATE_PULL_FAILED.format(target=resolved), file=sys.stderr)
                return EXIT_FAILED_ROLLED_BACK  # _disarm() (finally) restores refs
            up = compose.compose_up(compose_file, profile=profile)
            if not up.ok:
                return _rollback(
                    compose_file, profile=profile, services=services,
                    old_refs=old_refs, greffer_id=greffer_id, timeout=timeout, sleep=sleep,
                )

            # ---- 4. Health-gate --------------------------------------
            outcome = health_gate(
                compose_file, greffer_id=greffer_id, target=resolved,
                services=services, profile=profile, timeout=timeout, sleep=sleep,
            )
            if outcome == GATE_READY:
                gate_passed = True
                print(strings.UPDATE_OK.format(target=resolved))
                return EXIT_OK

            # ---- 5. Rollback (the authority) -------------------------
            print(strings.UPDATE_GATE_FAILED.format(reason=outcome), file=sys.stderr)
            return _rollback(
                compose_file, profile=profile, services=services,
                old_refs=old_refs, greffer_id=greffer_id, timeout=timeout, sleep=sleep,
            )
        finally:
            _disarm()
            _restore_sigterm(prev_sigterm)
    finally:
        _release_update_lock(lock)
