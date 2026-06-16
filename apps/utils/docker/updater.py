"""Spawn the detached ``greffon/greffer-updater`` container (self-update v2).

A manager-triggered remote update cannot run in the greffer process: the
updater recreates the greffer's OWN container, so it has to outlive the process
it replaces. The controller therefore spawns a SEPARATE, short-lived container
from the signed, digest-pinned updater image, mounting the docker socket plus
the same host paths the greffer itself uses, and returns 202 right away. The
updater then takes the ``/data`` lock, verifies provenance, and recreates the
stack (see ``greffer_cli.updater``).

Host-path discovery: inside a container the bind sources visible on ``/proc``
are CONTAINER paths; to mount the same host directories into a SIBLING
container we need the HOST sources. We read them off the greffer's own
container record (looked up by ``hostname`` == container id), not from env, so
``/work`` (the compose dir) and ``/data`` always track however the operator
actually mounted the greffer. Reusing the recorded mount ``Type``/``Source``
also preserves a named-volume ``/data`` as a volume mount rather than guessing a
host path.

Fail-closed: any missing mount, unknown self-container, or docker error raises
``UpdaterSpawnError`` and the route surfaces it; nothing is spawned half-wired.
"""

from __future__ import annotations

import logging
import socket

import docker
from docker.types import Mount

logger = logging.getLogger("greffer")

# The greffer mounts its compose dir at /app (``./:/app``) and uses
# ``$GREFFON_PATH`` (default /data) for persistent state. The updater image
# expects the compose dir at /work and the shared state at /data.
SELF_COMPOSE_DEST = "/app"
UPDATER_COMPOSE_DEST = "/work"
DOCKER_SOCK = "/var/run/docker.sock"


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
            return Mount(target=target, source=source, type=mtype,
                         read_only=False)
    raise UpdaterSpawnError(
        f"greffer has no mount at {destination}; cannot wire the updater")


def spawn_updater(
    *, image: str, target_tag: str, manifest_url: str, greffer_id: str | None,
    mode: str, data_dest: str = "/data", client=None,
) -> str:
    """Spawn the detached updater container and return its id.

    ``image`` MUST be a digest-pinned ref (caller's responsibility / settings
    contract). Mounts: the greffer's compose dir -> /work, its ``data_dest``
    (default /data) -> /data, and the docker socket. The target tag is passed as
    a list arg (no shell), and the updater re-validates it; provenance/floor
    verification all happen inside the container before any recreate."""
    if not image:
        raise UpdaterSpawnError("no updater image configured")
    client = client or docker.from_env()
    self_attrs = _self_container(client).attrs

    mounts = [
        _mount_for(self_attrs, SELF_COMPOSE_DEST, target=UPDATER_COMPOSE_DEST),
        _mount_for(self_attrs, data_dest, target="/data"),
        Mount(target=DOCKER_SOCK, source=DOCKER_SOCK, type="bind"),
    ]
    environment = {
        "GREFFER_VERSION_MANIFEST_URL": manifest_url,
        "GREFFER_ID": greffer_id or "",
        "GREFFER_MODE": mode,
    }
    try:
        container = client.containers.run(
            image,
            ["python", "-m", "greffer_cli.updater", target_tag],
            detach=True,
            # The updater is one-shot; drop the container when it exits so a
            # series of updates doesn't litter the host. Logs are captured by
            # the manager-visible exit, not the container record.
            remove=True,
            environment=environment,
            mounts=mounts,
            # Default bridge: the updater needs egress to fetch the signed
            # version manifest (HTTPS) and to resolve image digests at the
            # registry (``buildx imagetools inspect`` / cosign run in-container).
            # It exposes no inbound port.
        )
    except docker.errors.APIError as exc:
        raise UpdaterSpawnError(f"failed to spawn updater: {exc}") from exc
    logger.info("remote_update_spawned image=%s target=%s container=%s",
                image, target_tag, container.id[:12])
    return container.id
