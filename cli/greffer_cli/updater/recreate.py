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
