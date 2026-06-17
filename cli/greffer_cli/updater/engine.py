"""The v2 ``:latest`` updater orchestration: verify-then-pull -> recreate -> gate.

Socket-only (no compose file, HLD ``hld-v2-per-container-recreate.md``): discover
the running greffon stack by compose service label (section 4), verify-then-pull
EVERY ``greffon/*`` image by its cosign-verified digest fail-closed (sections 3 +
13, so a verification failure recreates nothing), then per container in order
(section 6) name the outgoing image ``:previous`` (section 9), point ``:latest``
at the verified digest, and recreate carrying config per the section 8 fidelity
rule. Health-gate ``/readyz`` over the socket (section 9); on success prune
dangling images (section 5) after a cheap anti-downgrade guard (section 13); on
any failure roll the whole stack back to the captured prior images.

Runs inside the signed ``greffon/greffer-updater`` container against the host
docker socket. The caller (the entrypoint) holds ``/data/.update.lock`` so a
remote update and a host ``greffer update`` are mutually exclusive.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .. import compose, update
from . import gate, recreate

logger = logging.getLogger("greffer-updater")

EXIT_OK = update.EXIT_OK
EXIT_FAILED_ROLLED_BACK = update.EXIT_FAILED_ROLLED_BACK
EXIT_FAILED_ROLLBACK_FAILED = update.EXIT_FAILED_ROLLBACK_FAILED
EXIT_REFUSED = update.EXIT_PREFLIGHT_REFUSED

GREFFER_SERVICE = "greffer"


def _is_downgrade(new: str | None, old: str | None) -> bool:
    """True iff ``new`` < ``old`` by dotted-numeric compare. The anti-downgrade
    guard (HLD section 13): catches an honest-CI ``:latest`` force-pushed to an
    older build. NOT a trust control (``app.__version__`` is attacker-forgeable,
    section 7), so non-numeric versions are treated as not-a-downgrade."""
    def parse(v: str | None):
        if not v:
            return None
        parts = []
        for p in v.split("."):
            if not p.isdigit():
                return None
            parts.append(int(p))
        return tuple(parts)
    pn, po = parse(new), parse(old)
    if pn is None or po is None:
        return False
    return pn < po


def _rollback(done: list, *, greffer_name: str, greffer_id: str | None,
              service_names: list[str], timeout: float,
              sleep: Callable[[float], None], now: Callable[[], float]) -> int:
    """Roll every recreated container (reverse order) back to its captured prior
    image, then re-gate (no version-applied check, rollback restores the prior
    version). ``done`` = ``[(container, inspected, old_image_id), ...]`` in
    recreate order. Returns ``EXIT_FAILED_ROLLED_BACK`` if the rolled-back stack
    is healthy, else ``EXIT_FAILED_ROLLBACK_FAILED`` (manual recovery)."""
    all_ok = True
    for container, inspected, old_image_id in reversed(done):
        if not old_image_id:
            logger.error("rollback %s: no captured old image id", container.service)
            all_ok = False
            continue
        if not recreate.rollback_one(container, inspected, old_image_id):
            all_ok = False
    if all_ok and greffer_name and gate.health_gate(
        greffer_name, greffer_id=greffer_id, applied_image_id=None,
        check_version=False, service_names=service_names,
        timeout=min(timeout, 120.0), sleep=sleep, now=now,
    ) == update.GATE_READY:
        logger.info("rolled back to the prior images")
        return EXIT_FAILED_ROLLED_BACK
    logger.error("ROLLBACK FAILED, manual recovery needed")
    return EXIT_FAILED_ROLLBACK_FAILED


def run_remote_update(
    *, cosign_pub: str, greffer_id: str | None, timeout: float = 600.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """Run the socket-only ``:latest`` update. Returns an ``EXIT_*`` code.
    Nothing is recreated unless every image verifies."""
    stack = recreate.discover_stack()
    if not stack:
        logger.error("remote update refused: could not discover the greffon stack")
        return EXIT_REFUSED
    greffer = next((c for c in stack if c.service == GREFFER_SERVICE), None)
    if greffer is None:
        logger.error("remote update refused: greffer not in the discovered stack")
        return EXIT_REFUSED

    # Phase 1: verify + pull every image by digest, fail-closed. No :latest is
    # moved and nothing is recreated, so a failure here is a clean no-op refusal.
    try:
        verified = {c.repo: recreate.verify_and_pull(c.repo, cosign_pub=cosign_pub)
                    for c in stack}
    except recreate.VerifyError as exc:
        logger.error("remote update refused, no recreate: %s", exc)
        return EXIT_REFUSED

    old_version = recreate.exec_version(greffer.container_id)

    # Phase 2a: snapshot every container BEFORE mutating anything (so a failed
    # inspect is a clean refusal, and rollback can reuse the pre-update inspect).
    snaps: list[tuple] = []  # (container, inspected, name, old_image_id)
    for c in stack:
        inspected = recreate.inspect_container(c.container_id)
        if not inspected:
            logger.error("remote update refused: cannot inspect %s (nothing mutated yet)",
                         c.service)
            return EXIT_REFUSED
        name = (inspected.get("Name") or "").lstrip("/")
        old_image_id = inspected.get("Image") or recreate.current_image_id(c.repo)
        snaps.append((c, inspected, name, old_image_id))
    service_names = [name for _, _, name, _ in snaps]
    greffer_name = next((name for c, _, name, _ in snaps if c.service == GREFFER_SERVICE), "")

    # Phase 2b: name :previous, point :latest at the verified digest, recreate.
    done: list[tuple] = []  # (container, inspected, old_image_id)
    for c, inspected, name, old_image_id in snaps:
        recreate.tag_previous(c.repo, old_image_id)
        if not recreate.retag_latest(c.repo, verified[c.repo]):
            logger.error("remote update: retag :latest failed for %s, rolling back", c.repo)
            return _rollback(done, greffer_name=greffer_name, greffer_id=greffer_id,
                             service_names=service_names, timeout=timeout, sleep=sleep, now=now)
        ok = recreate.recreate_one(
            c, image_ref=f"{c.repo}:latest", old_image_id=old_image_id, inspected=inspected)
        done.append((c, inspected, old_image_id))
        if not ok:
            logger.error("remote update: recreate %s failed, rolling back", c.service)
            return _rollback(done, greffer_name=greffer_name, greffer_id=greffer_id,
                             service_names=service_names, timeout=timeout, sleep=sleep, now=now)

    # Phase 3: gate the recreated stack on /readyz.
    applied = compose.image_id(f"{greffer.repo}@{verified[greffer.repo]}")
    outcome = gate.health_gate(
        greffer_name, greffer_id=greffer_id, applied_image_id=applied,
        service_names=service_names, timeout=timeout, sleep=sleep, now=now)
    if outcome == update.GATE_READY:
        if _is_downgrade(recreate.exec_version(greffer_name), old_version):
            logger.error("remote update: version went backward from %s, rolling back",
                         old_version)
            return _rollback(done, greffer_name=greffer_name, greffer_id=greffer_id,
                             service_names=service_names, timeout=timeout, sleep=sleep, now=now)
        recreate.dangling_prune()
        logger.info("remote update applied")
        return EXIT_OK

    logger.error("remote update: health gate failed (%s), rolling back", outcome)
    return _rollback(done, greffer_name=greffer_name, greffer_id=greffer_id,
                     service_names=service_names, timeout=timeout, sleep=sleep, now=now)
