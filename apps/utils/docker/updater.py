"""Spawn the detached ``greffon/greffer-updater`` container (self-update v2).

A manager-triggered remote update cannot run in the greffer process: the
updater recreates the greffer's OWN container, so it has to outlive the process
it replaces. The controller spawns a SEPARATE, short-lived container from the
signed, digest-pinned updater image, mounting ONLY the docker socket and the
greffer's ``/data`` volume (socket-only model: no compose file, no compose-dir
mount), and returns 202 right away. The updater then takes the
``/data/.update.lock``, verify-then-pulls each image at the target version, and
recreates the stack per container (see ``greffer_cli.updater``).

``/data`` host-source discovery: inside a container the bind sources on
``/proc`` are CONTAINER paths; to mount the same host ``/data`` into a SIBLING
container we read the source off the greffer's own container record (looked up
by ``hostname`` == container id), preserving a named-volume ``/data`` as a
volume mount rather than guessing a host path.

Fail-closed: any missing ``/data`` mount, unknown self-container, or docker
error raises ``UpdaterSpawnError`` and the route surfaces it; nothing is spawned
half-wired.
"""

from __future__ import annotations

import logging
import re
import socket

import docker
from docker.types import Mount

logger = logging.getLogger("greffer")

# The updater image MUST be digest-pinned: it is the one root-equivalent,
# socket-mounted container that recreates the greffer, and it is NOT itself run
# through the cosign verification it performs on the target. A mutable tag
# (``:latest``) could be repointed registry-side to swap it. Require an explicit
# ``@sha256:<64 hex>`` so a tag move can never change what runs. Anchored with
# fullmatch so a pathological ref with a second ``@sha256:`` or trailing junk
# cannot slip past.
_DIGEST_PINNED_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")

DOCKER_SOCK = "/var/run/docker.sock"


def is_digest_pinned(ref: object) -> bool:
    """True iff ``ref`` is an image reference pinned by a sha256 digest."""
    return isinstance(ref, str) and _DIGEST_PINNED_RE.fullmatch(ref) is not None


def update_in_progress(lock_path) -> bool:
    """True iff a self-update currently holds the ``/data`` update lock.

    The controller probes this (non-blocking ``flock``) on start/stop/update so a
    manager action cannot mutate instance/compose state in the window the updater
    is recreating the stack (HLD section 10). The probe acquires-then-releases, so
    it never holds the lock the updater needs; a held lock (the updater is
    mid-update) makes the acquire fail -> True. On a platform without fcntl, or if
    the lock file cannot be opened, returns False (cannot tell -> do not block)."""
    try:
        import fcntl
    except ImportError:
        return False
    try:
        fh = open(lock_path, "a", encoding="utf-8")  # "a": create, never truncate
    except OSError:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


class UpdaterSpawnError(Exception):
    """The updater container could not be wired/spawned. Fail-closed."""


def _self_container(client):
    """The greffer's own container, looked up by hostname (== container id
    inside docker). Raises UpdaterSpawnError if it can't be resolved."""
    name = socket.gethostname()
    try:
        return client.containers.get(name)
    except docker.errors.NotFound as exc:
        raise UpdaterSpawnError(
            f"cannot resolve own container (hostname={name!r}); "
            "is the greffer running under docker with the socket mounted?"
        ) from exc
    except docker.errors.APIError as exc:
        raise UpdaterSpawnError(f"docker error resolving self: {exc}") from exc


def _mount_for(self_attrs: dict, destination: str, *, target: str) -> Mount:
    """Replicate the self-container's mount at ``destination`` onto a new
    ``target``, preserving the mount Type (bind vs named volume) and Source.
    Raises UpdaterSpawnError if the greffer has no such mount."""
    for m in self_attrs.get("Mounts", []):
        if m.get("Destination") == destination:
            mtype = m.get("Type", "bind")
            # For a named volume the reusable source is the volume Name; for a
            # bind it is the host path in Source.
            source = m.get("Name") if mtype == "volume" else m.get("Source")
            if not source:
                raise UpdaterSpawnError(
                    f"mount at {destination} has no reusable source")
            return Mount(target=target, source=source, type=mtype, read_only=False)
    raise UpdaterSpawnError(
        f"greffer has no mount at {destination}; cannot wire the updater")


def spawn_updater(
    *, image: str, target_tag: str | None, greffer_id: str | None,
    data_dest: str = "/data", client=None,
) -> str:
    """Spawn the detached updater container and return its id.

    ``image`` MUST be a digest-pinned ref (the route and this both enforce it).
    Mounts: the greffer's ``data_dest`` (default ``/data``) -> ``/data`` and the
    docker socket; NOTHING else (socket-only, no compose dir). ``target_tag`` (the
    server-resolved version; latest when None) is passed as
    ``GREFFER_UPDATER_TARGET_TAG``, which the updater re-validates before running
    the in-container verify -> recreate -> health-gate -> rollback flow."""
    if not image:
        raise UpdaterSpawnError("no updater image configured")
    if not is_digest_pinned(image):
        # Defense-in-depth: the route also rejects this, but enforce it here so
        # the contract holds for any caller of spawn_updater.
        raise UpdaterSpawnError(
            f"updater image must be digest-pinned (got {image!r})")
    client = client or docker.from_env()
    self_attrs = _self_container(client).attrs

    mounts = [
        _mount_for(self_attrs, data_dest, target="/data"),
        Mount(target=DOCKER_SOCK, source=DOCKER_SOCK, type="bind"),
    ]
    environment = {"GREFFER_ID": greffer_id or ""}
    if target_tag:
        environment["GREFFER_UPDATER_TARGET_TAG"] = target_tag
    try:
        container = client.containers.run(
            image,
            ["python", "-m", "greffer_cli.updater"],
            detach=True,
            # One-shot; drop the container when it exits so a series of updates
            # doesn't litter the host.
            remove=True,
            environment=environment,
            mounts=mounts,
            # Default bridge: the updater needs egress to resolve image digests
            # and pull (buildx imagetools inspect / cosign / docker pull run
            # in-container). It exposes no inbound port.
        )
    except docker.errors.APIError as exc:
        raise UpdaterSpawnError(f"failed to spawn updater: {exc}") from exc
    logger.info("remote_update_spawned image=%s target=%s container=%s",
                image, target_tag or "latest", container.id[:12])
    return container.id
