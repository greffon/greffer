"""The v2 updater orchestration: verify -> pin -> recreate.

Runs inside the signed ``greffon/greffer-updater`` container against the host's
``/work/docker-compose.yml``. Adds the trust-model verification (effective
``min_supported`` floor + per-image cosign verify / repo-binding / digest-pin /
version cohesion) BEFORE any recreate, then reuses the v1 ``greffer_cli`` engine
for recreate -> ``/readyz`` health-gate -> rollback. Fail-closed at every step:
any verification or floor failure aborts before the node is recreated.

Concurrency: the caller (the updater entrypoint) holds the
``/data/.greffer-update.lock`` across this call so the recreated controller and a
host ``greffer update`` are mutually exclusive (HLD "Concurrency"). The lock is
out of this module's scope so the verify/recreate logic stays unit-testable.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from .. import compose, update
from . import floor, provenance

logger = logging.getLogger("greffer-updater")

# Exit codes mirror the v1 engine so a wrapper reads them the same way.
EXIT_OK = update.EXIT_OK
EXIT_FAILED_ROLLED_BACK = update.EXIT_FAILED_ROLLED_BACK
EXIT_FAILED_ROLLBACK_FAILED = update.EXIT_FAILED_ROLLBACK_FAILED
EXIT_REFUSED = update.EXIT_PREFLIGHT_REFUSED


class VerifyError(Exception):
    """A provenance check failed. Fail-closed: refuse before recreate."""


def _compose_repos(compose_file: Path) -> dict[str, str]:
    """The ``greffon/*`` repos in the rendered compose mapped to their current
    full refs (for rollback). Derived from the file so it tracks whatever the
    template renders (the set-closure invariant)."""
    text = compose_file.read_text(encoding="utf-8")
    _, refs = compose._rewrite_image_lines(text, lambda repo, old: None)
    return refs


def verify_and_pin(
    compose_file: Path, *, target_tag: str, manifest_url: str,
    cosign_pub: str, baked_baseline: str | None, ratchet_path: Path,
) -> dict[str, str]:
    """Run the full trust model and return the verified ``repo -> repo@digest``
    refs to pin. Raises ``VerifyError`` / ``floor.FloorError`` fail-closed on any
    failure, leaving the compose file untouched (no recreate)."""
    if not compose.is_valid_image_tag(target_tag):
        raise VerifyError(f"invalid target tag: {target_tag!r}")

    # Effective floor first (max of baked baseline, signed manifest, ratchet);
    # FloorError aborts before any image work.
    floor_v = floor.effective_floor(
        manifest_url, baked_baseline=baked_baseline,
        ratchet_path=ratchet_path, cosign_pub=cosign_pub,
    )

    repos = _compose_repos(compose_file)
    if not repos:
        raise VerifyError("no greffon/* images in the compose file")

    verified: dict[str, str] = {}
    versions: set[str] = set()
    for repo in repos:
        ref = f"{repo}:{target_tag}"
        digest = provenance.resolve_digest(ref)
        if not digest:
            raise VerifyError(f"cannot resolve digest for {ref}")
        if not provenance.cosign_verify(repo, digest, pubkey=cosign_pub):
            raise VerifyError(f"signature/repo-binding failed for {repo}@{digest}")
        by_digest = f"{repo}@{digest}"
        # Pull by the verified digest so the OCI version label is locally
        # readable; pulling bytes is safe, recreating them is what the floor gates.
        if not compose._run(["docker", "pull", by_digest], timeout=600).ok:
            raise VerifyError(f"pull failed for {by_digest}")
        version = provenance.image_version(by_digest)
        if not floor.version_ge(version, floor_v):
            raise VerifyError(
                f"{repo} version {version!r} is below the floor {floor_v}")
        versions.add(version)  # type: ignore[arg-type]
        verified[repo] = by_digest

    # Cohesion: every image must be the SAME version, so a moved tag can't pair
    # an at-floor greffer with a below-floor, network-exposed nginx.
    if len(versions) != 1:
        raise VerifyError(f"version cohesion failed across images: {sorted(versions)}")
    return verified


def run_remote_update(
    compose_file: Path, *, target_tag: str, manifest_url: str,
    cosign_pub: str, baked_baseline: str | None, ratchet_path: Path,
    greffer_id: str | None, mode: str, timeout: float = 600.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Verify provenance + floor, pin the verified digests, recreate,
    health-gate on ``/readyz``, and roll back on failure. Returns an ``EXIT_*``
    code. Nothing is recreated unless verification fully passes."""
    old_refs = _compose_repos(compose_file)
    try:
        verified = verify_and_pin(
            compose_file, target_tag=target_tag, manifest_url=manifest_url,
            cosign_pub=cosign_pub, baked_baseline=baked_baseline,
            ratchet_path=ratchet_path,
        )
    except (VerifyError, floor.FloorError) as exc:
        logger.error("remote update refused, no recreate: %s", exc)
        return EXIT_REFUSED

    profile = update.profile_for_mode(mode)
    services = update.active_services(mode)
    compose.set_image_refs(compose_file, verified)  # pin the verified digests
    up = compose.compose_up(compose_file, profile=profile)
    if not up.ok:
        return update._rollback(
            compose_file, profile=profile, services=services,
            old_refs=old_refs, greffer_id=greffer_id, timeout=timeout, sleep=sleep)

    # v2 pins/pulls by digest, so the local ``greffon/greffer:<tag>`` tag never
    # exists; tell the gate to verify "version applied" against the VERIFIED
    # greffer digest instead, or it reads None and rolls back every success.
    outcome = update.health_gate(
        compose_file, greffer_id=greffer_id, target=target_tag,
        services=services, profile=profile, timeout=timeout, sleep=sleep,
        applied_ref=verified.get(update.GREFFER_REPO))
    if outcome == update.GATE_READY:
        logger.info("remote update applied: %s", target_tag)
        return EXIT_OK

    logger.error("health gate failed (%s), rolling back", outcome)
    return update._rollback(
        compose_file, profile=profile, services=services,
        old_refs=old_refs, greffer_id=greffer_id, timeout=timeout, sleep=sleep)
