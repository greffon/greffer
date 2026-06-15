"""Wrapper around ``docker`` and ``docker compose`` subprocess calls.

Every exec call uses ``-T`` to disable TTY allocation — without it,
``docker compose exec`` defaults to interactive TTY and fails in
non-interactive subprocess contexts (the CLI is one, CI is another)
with "the input device is not a TTY".
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run(args: list[str], *, timeout: float | None = None) -> CommandResult:
    """Run a subprocess; never raise on non-zero exit. Caller inspects."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        # docker isn't installed; surface as a non-zero result the
        # caller can interpret as "docker missing."
        return CommandResult(returncode=127, stdout="", stderr=f"command not found: {args[0]}")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=124, stdout=exc.stdout or "", stderr=f"timeout after {timeout}s",
        )
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


# --- Docker daemon / installation -----------------------------------

def docker_cli_installed() -> CommandResult:
    """``docker --version`` — daemon-independent. Tests "Docker CLI binary
    is on PATH"; does NOT contact the daemon. The dedicated daemon check
    uses ``docker info``.

    Note: ``docker version`` (no double-dash) contacts the daemon and
    fails when the daemon is down — we deliberately use the static
    ``docker --version`` here so a "daemon down" condition reports as
    "daemon not reachable" not "Docker not installed."
    """
    return _run(["docker", "--version"], timeout=10)


def docker_version() -> CommandResult:
    """``docker version --format json`` — full client + server info.

    Hits the daemon; will fail if the daemon is down. Use
    ``docker_cli_installed()`` for daemon-independent installation
    detection.
    """
    return _run(["docker", "version", "--format", "json"], timeout=10)


def docker_info() -> CommandResult:
    """``docker info`` — used by doctor to verify the daemon is reachable."""
    return _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)


def docker_compose_version() -> CommandResult:
    """``docker compose version --short`` — daemon-independent.

    The compose plugin is invoked as a docker subcommand but doesn't
    contact the daemon for ``version --short``; this works on a host
    where the daemon is down.
    """
    return _run(["docker", "compose", "version", "--short"], timeout=10)


# --- Compose lifecycle ----------------------------------------------

def compose_up(compose_file: Path, *, profile: str | None = None) -> CommandResult:
    """Bring the compose stack up. In tunnel mode, pass profile="tunnel"
    so the tunnel-sidecar service starts; in proxy mode (default) only
    the greffer + nginx services start.

    Profiles are how the single bundled compose.yml supports both modes:
    services with ``profiles: ["tunnel"]`` are skipped unless that
    profile is enabled.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    if profile:
        args.extend(["--profile", profile])
    args.extend(["up", "-d"])
    return _run(args, timeout=300)  # image pull can be slow


def compose_ps(compose_file: Path, *, profile: str | None = None) -> CommandResult:
    """List compose services, including stopped ones.

    Mode-aware via profile: without the profile, ``ps`` only lists
    default services even if a profiled service is also running. We
    pass the profile so tunnel-mode operators see all containers
    (greffer + nginx + tunnel-sidecar).

    We also pass ``--all``: by default ``docker compose ps`` only shows
    RUNNING services (per Docker's CLI docs), so a crashed/exited
    service would disappear entirely — making ``wait_for_compose_running``
    falsely report success because the failed service isn't in the
    dict to check. With ``--all`` it shows up with state != "running",
    which the caller correctly flags as not-yet-up.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    if profile:
        args.extend(["--profile", profile])
    args.extend(["ps", "--all", "--format", "json"])
    return _run(args, timeout=15)


def compose_services_running(
    compose_file: Path, *, profile: str | None = None,
) -> dict[str, bool]:
    """Parse ``compose ps --format json`` and return a dict of service → running.

    Compose v2 emits one JSON object per line (NDJSON-style). v1 emitted
    a single JSON array. We accept either. Pass ``profile`` to surface
    profiled services (e.g., the tunnel-sidecar in tunnel mode).
    """
    result = compose_ps(compose_file, profile=profile)
    if not result.ok:
        return {}
    text = result.stdout.strip()
    if not text:
        return {}
    # Be defensive about two known stdout shapes:
    #   1. Compose v2 NDJSON: one JSON object per line.
    #   2. Compose v1 (and v2 with `--format json` per Docker docs): a
    #      single JSON array.
    # AND about a compose-plugin warning printed before the payload —
    # which would push the array onto a non-first line and was
    # previously appended as a single nested list, crashing item.get()
    # below. Extend on lists, append on dicts.
    services: dict[str, bool] = {}
    items: list[dict] = []
    try:
        if text.startswith("["):
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items.extend(parsed)
        else:
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue  # warning / banner line; skip
                if isinstance(parsed, list):
                    items.extend(parsed)
                elif isinstance(parsed, dict):
                    items.append(parsed)
                # Any other JSON scalar is ignored — compose has never
                # emitted one in practice but we don't want to crash if
                # the format ever drifts.
    except json.JSONDecodeError:
        return {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Service") or item.get("Name")
        state = item.get("State") or item.get("status", "")
        if name:
            services[name] = state.lower() == "running"
    return services


# --- Local exec into the running greffer / nginx --------------------

def exec_in_greffer_healthz(compose_file: Path) -> CommandResult:
    """Probe the FastAPI app's ``/healthz`` from inside the greffer container.

    We hit the app's internal port (8000) from inside the container
    rather than the host's exposed nginx port — the host probe is the
    reachability self-test (proxy mode only) and depends on operator
    DNS / public-host setup; the in-container probe just verifies the
    app is up.

    We use ``python -c urllib.request`` rather than ``curl``: the
    greffer image is ``python:3.11-alpine`` and does NOT install curl
    (see greffer/Dockerfile). Python is guaranteed present — it's
    what runs uvicorn. Exit 0 iff the response status is 200.
    """
    # Catch HTTPError / URLError explicitly: ``urlopen`` raises
    # ``HTTPError`` for 4xx/5xx (so a 503 would never reach ``r.status``)
    # and ``URLError`` for connection refused. Without the try/except,
    # those manifest as a traceback piped through ``docker exec`` —
    # functionally non-zero exit, but noisy in logs. Clean exit 1 in
    # both error cases.
    probe = (
        "import sys, urllib.request, urllib.error;"
        "\ntry:"
        "\n    r = urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)"
        "\n    sys.exit(0 if r.status == 200 else 1)"
        "\nexcept (urllib.error.HTTPError, urllib.error.URLError):"
        "\n    sys.exit(1)"
    )
    return _run(
        [
            "docker", "compose", "-f", str(compose_file),
            "exec", "-T", "greffer",
            "python", "-c", probe,
        ],
        timeout=10,
    )


def exec_nginx_cert_installed(compose_file: Path) -> CommandResult:
    """Check that the cert was installed into the nginx sidecar.

    ``test -s`` verifies the file exists AND is non-empty (guards
    against placeholder / pre-write states). Only meaningful in proxy
    mode — tunnel mode uses a Stem-client sidecar instead, with a
    cert-installed signal that's TBD per the Stem HLD.
    """
    return _run(
        [
            "docker", "compose", "-f", str(compose_file),
            "exec", "-T", "nginx",
            "test", "-s", "/root/pem.crt",
        ],
        timeout=10,
    )


def docker_inspect_restart_count(container_id: str) -> int:
    """Return the container's RestartCount, or 0 if unavailable."""
    result = _run(
        ["docker", "inspect", container_id, "--format", "{{.RestartCount}}"],
        timeout=10,
    )
    if not result.ok:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


# --- Update engine: pull, image-ref rewrite, readiness, digests -----

def compose_pull(
    compose_file: Path, *, profile: str | None = None,
    services: list[str] | None = None,
) -> CommandResult:
    """``docker compose pull`` the node images before a recreate.

    A plain ``up`` reuses the locally-cached image when only the tag
    changed in the compose file, so an explicit pull is required to
    actually fetch the target. Pass explicit ``services`` to pull (and
    thereby validate) even profile-gated ones (the ``tunnel-sidecar`` in
    proxy mode): an update rewrites its image line too, so a missing or
    bad target tag must be caught here rather than silently leaving an
    inactive service pointed at an unpullable image.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    if profile:
        args.extend(["--profile", profile])
    args.append("pull")
    if services:
        args.extend(services)
    return _run(args, timeout=300)


# An ``image:`` line for one of our published node images. Matches a
# bare repo, a tagged ref, or a digest-pinned ref (a prior rollback may
# have left ``greffon/greffer@sha256:...``). The compose template keeps
# each image on its own line with no trailing inline comment, so a
# line-oriented rewrite is safe and avoids a YAML round-trip that would
# reorder keys / strip comments.
_IMAGE_LINE_RE = re.compile(
    r"^(?P<indent>\s*image:\s*)"
    r"(?P<repo>greffon/[A-Za-z0-9._-]+)"
    r"(?::[^\s@]+|@sha256:[0-9a-fA-F]+)?"
    r"(?P<trail>\s*)$"
)


def _rewrite_image_lines(text: str, resolve) -> tuple[str, dict[str, str]]:
    """Rewrite every ``greffon/*`` image line via ``resolve(repo, old_ref)``.

    ``resolve`` returns the new full ref (e.g. ``greffon/greffer:0.3.4``
    or ``greffon/greffer@sha256:...``) or ``None`` to leave the line
    unchanged. Returns ``(new_text, old_refs)`` where ``old_refs`` maps
    repo -> the full ref that was there before (for rollback).
    """
    out: list[str] = []
    old_refs: dict[str, str] = {}
    for line in text.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        m = _IMAGE_LINE_RE.match(line.rstrip("\n"))
        if not m:
            out.append(line)
            continue
        repo = m.group("repo")
        old_full = m.group("indent").strip() and line.strip()[len("image:"):].strip()
        old_refs[repo] = old_full or repo
        new_ref = resolve(repo, old_refs[repo])
        if new_ref is None:
            out.append(line)
        else:
            out.append(f"{m.group('indent')}{new_ref}{nl}")
    return "".join(out), old_refs


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.tmp.")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def set_image_tag(compose_file: Path, target: str) -> dict[str, str]:
    """Rewrite **every** ``greffon/*`` image line to ``<repo>:<target>``.

    Covers greffer + greffer-nginx + the profile-gated tunnel-sidecar:
    the set is derived from the file, not a hard-coded count, so a node
    update retags the whole node whatever the mode renders. Returns the
    map of repo -> prior full ref, so a rollback can restore the exact
    refs that were there before (`set_image_refs`).
    """
    text = compose_file.read_text(encoding="utf-8")
    new_text, old_refs = _rewrite_image_lines(
        text, lambda repo, _old: f"{repo}:{target}",
    )
    _atomic_write(compose_file, new_text)
    return old_refs


def set_image_refs(compose_file: Path, refs: dict[str, str]) -> None:
    """Rewrite ``greffon/*`` image lines to the given full refs.

    ``refs`` maps repo -> full image ref (tag or digest). Lines whose
    repo is absent from ``refs`` are left untouched. Used by rollback to
    restore captured prior refs, pinning active services to their
    captured running digest where a moved tag would otherwise resolve
    to the bad image.
    """
    text = compose_file.read_text(encoding="utf-8")
    new_text, _ = _rewrite_image_lines(
        text, lambda repo, _old: refs.get(repo),
    )
    _atomic_write(compose_file, new_text)


# A compose short-syntax volume mount onto /data: ``- <source>:/data[:opts]``.
_DATA_MOUNT_RE = re.compile(r"^\s*-\s*(?P<src>[^\s:]+):/data(?::[a-zA-Z,]+)?\s*$")


def data_volume_is_named(compose_file: Path) -> bool:
    """Pre-flight: is /data backed by a NAMED volume (not a bind / nothing)?

    A named volume (``greffon-data:/data``) survives a container recreate,
    which is what keeps the persisted token / TLS key / migration ledger
    and so the greffer's identity. A bind mount (``/host/path:/data``) or
    no /data mount at all fails this check: an update that recreates the
    container would otherwise drop identity and `409` on re-register.

    The discriminator without a YAML dependency: a named-volume source is
    a bare name (no path separator, not relative/absolute/home), whereas a
    bind-mount source is a path. The CLI always renders the short syntax,
    so a line match is sufficient and avoids a YAML round-trip.
    """
    try:
        text = compose_file.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        m = _DATA_MOUNT_RE.match(line)
        if m:
            src = m.group("src")
            return "/" not in src and not src.startswith((".", "~"))
    return False


def exec_in_greffer_readyz(compose_file: Path) -> CommandResult:
    """Probe ``/readyz`` from inside the greffer container, returning its body.

    Unlike ``exec_in_greffer_healthz`` (which only maps a 200 to exit 0),
    this returns the parsed-able JSON body on stdout so the caller can
    read ``id`` / ``status`` / ``reasons``. ``/readyz`` is authed, so the
    probe resolves ``X-GREFFON-TOKEN`` the way the service does (env
    ``GREFFER_TOKEN`` then the persisted ``/data/.greffer-token``) and
    sends it. The token never leaves the container: the CLI only reads
    the response body. Exit 0 on any HTTP response (200 or 503 — both
    carry a JSON body the caller inspects); non-zero on a connection
    error so the caller keeps polling.
    """
    probe = (
        "import sys, os, urllib.request, urllib.error\n"
        "tok = os.environ.get('GREFFER_TOKEN')\n"
        "if not tok:\n"
        "    try:\n"
        "        tok = open('/data/.greffer-token', encoding='utf-8').read().strip()\n"
        "    except OSError:\n"
        "        tok = ''\n"
        "req = urllib.request.Request(\n"
        "    'http://localhost:8000/readyz',\n"
        "    headers={'X-GREFFON-TOKEN': tok},\n"
        ")\n"
        "try:\n"
        "    r = urllib.request.urlopen(req, timeout=5)\n"
        "    sys.stdout.write(r.read().decode()); sys.exit(0)\n"
        "except urllib.error.HTTPError as e:\n"
        "    sys.stdout.write(e.read().decode()); sys.exit(0)\n"
        "except urllib.error.URLError:\n"
        "    sys.exit(1)\n"
    )
    return _run(
        [
            "docker", "compose", "-f", str(compose_file),
            "exec", "-T", "greffer",
            "python", "-c", probe,
        ],
        timeout=15,
    )


def image_id(ref: str) -> str | None:
    """Return the content image ID (``sha256:...``) of an image ref, or None.

    Used to (a) verify the recreate actually advanced to the pulled
    target — the running greffer's image ID must equal the target ref's
    image ID — and (b) detect a no-op recreate (image ID unchanged from
    the captured prior).
    """
    result = _run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"], timeout=10,
    )
    if not result.ok:
        return None
    out = result.stdout.strip()
    return out or None


def container_image_id(compose_file: Path, service: str) -> str | None:
    """Return the image ID the named compose service's container is running."""
    cid = _run(
        ["docker", "compose", "-f", str(compose_file), "ps", "-q", service],
        timeout=10,
    )
    if not cid.ok or not cid.stdout.strip():
        return None
    result = _run(
        ["docker", "inspect", cid.stdout.strip(), "--format", "{{.Image}}"],
        timeout=10,
    )
    if not result.ok:
        return None
    out = result.stdout.strip()
    return out or None


def host_port_free(port: int) -> bool:
    """Doctor: is the given host port available for nginx to bind?"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def quote(args: list[str]) -> str:
    """Build a shell-readable command string for logging."""
    return " ".join(shlex.quote(a) for a in args)
