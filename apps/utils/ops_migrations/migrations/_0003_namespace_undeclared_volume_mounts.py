"""0003 — namespace UNDECLARED mount-source docker volumes by instance id.

Pairs with compose-containment Feature 3 in
`apps/utils/greffon/repository.py`: an undeclared named source (a service
mounting ``dd:/data`` with no top-level ``volumes:`` declaration) used to
pass through verbatim, so the docker volume was the raw name ``dd``; the
render now namespaces it as ``<instance_id>_dd`` like a declared one.

0001 only namespaced volumes it could see as DECLARED names in the rendered
compose's top-level block at the time it ran — and it is ledger-recorded, so
it never re-runs for instances whose composes were rendered afterwards. Any
undeclared source whose data still sits in the raw ``dd`` volume would mount
EMPTY on its next deploy under the new render, looking like data loss.

Same scan shape as 0001, scoped to what it missed: every top-level volume
key of each instance's rendered compose that does NOT start with the
instance id prefix. Non-destructive (old volumes left in place, operator
prunes after verifying), idempotent per item (target-exists skips).
"""
from __future__ import annotations

import logging
import os
import subprocess

import yaml

from ..base import Migration
from ..registry import register

logger = logging.getLogger("greffer.ops_migrations")


def _volume_exists(name: str) -> bool:
    res = subprocess.run(
        ["docker", "volume", "inspect", name],
        capture_output=True,
    )
    return res.returncode == 0


def _copy_volume(src: str, dst: str) -> None:
    logger.info(f"0003: copying volume {src} -> {dst}")
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{src}:/from:ro",
            "-v", f"{dst}:/to",
            "alpine:3.20",
            "sh", "-c", "cp -a /from/. /to/",
        ],
        check=True,
        capture_output=True,
    )


@register
class NamespaceUndeclaredVolumeMounts(Migration):
    id = "0003_namespace_undeclared_volume_mounts"
    description = (
        "Copy docker volumes for UNDECLARED mount sources (raw-name volumes "
        "0001's declared-name scan missed) into their new "
        "<instance_id>_<name> counterparts, so instances with undeclared "
        "mounts survive the Feature-3 namespacing change."
    )
    stop_on_failure = False

    def run(self, data_root: str) -> dict:
        summary = {"migrated": 0, "skipped": 0, "errors": 0}
        if not os.path.isdir(data_root):
            logger.info(f"0003: data root {data_root} does not exist; skipping")
            return summary

        for instance_id in sorted(os.listdir(data_root)):
            instance_dir = os.path.join(data_root, instance_id)
            compose_path = os.path.join(instance_dir, "docker-compose.yml")
            if not os.path.isfile(compose_path):
                continue
            if instance_id.startswith(".") or "/" in instance_id:
                continue

            try:
                with open(compose_path) as f:
                    compose = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as e:
                logger.warning(f"0003: skipping {compose_path}: {e}")
                continue
            volumes = compose.get("volumes")
            if not isinstance(volumes, dict):
                continue

            prefix = f"{instance_id}_"
            for effective in volumes:
                # Already namespaced (declared sources since 0001, the nginx
                # sidecar volume, and post-change renders): not ours.
                if effective.startswith(prefix):
                    summary["skipped"] += 1
                    continue
                expected = f"{instance_id}_{effective}"
                if not _volume_exists(effective):
                    # Never created — the next start creates the new one
                    # empty; nothing to copy.
                    summary["skipped"] += 1
                    continue
                if _volume_exists(expected):
                    # 0001 already made the copy, or a partial retry — never
                    # touch either side.
                    summary["skipped"] += 1
                    continue
                try:
                    subprocess.run(
                        ["docker", "volume", "create", expected],
                        check=True, capture_output=True,
                    )
                    _copy_volume(effective, expected)
                    summary["migrated"] += 1
                    logger.info(
                        f"0003: {instance_id}/{effective} copied "
                        f"{effective} -> {expected}"
                    )
                except subprocess.CalledProcessError as e:
                    summary["errors"] += 1
                    logger.error(
                        f"0003: failed {instance_id}/{effective}: "
                        f"{e.stderr.decode(errors='replace') if e.stderr else e}"
                    )
        return summary
