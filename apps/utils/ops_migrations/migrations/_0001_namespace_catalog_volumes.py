"""0001 — namespace catalog-declared docker volumes by greffon instance id.

Pairs with the volume-namespacing fix in `apps/utils/greffon/repository.py`.
Before that fix, the greffer wrote catalog-declared volumes to docker under
their raw compose-author labels (e.g. `db_data`). Multiple instances thus
shared the same host volume, causing silent data corruption.

After the fix, each instance's volumes are namespaced as `<instance_id>_<name>`.
Existing instances deployed under the old scheme still have their data in
the old shared volumes — they need a one-time copy into the new per-instance
volume names, or their next restart will come up empty and look like data
loss.

This migration scans the greffer's data root, identifies volumes still using
the raw label, and for each: creates a new namespaced docker volume and
copies the contents across via a short-lived alpine container. Old volumes
are left in place (non-destructive; operator can `docker volume prune` once
they've verified each greffon comes up cleanly).
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Iterable

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
    logger.info(f"0001: copying volume {src} -> {dst}")
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


def _volumes_declared_in_compose(compose_path: str) -> Iterable[tuple[str, str]]:
    """Yield (declared_name, effective_name_in_rendered_compose) for each
    top-level volume declared in compose_path. If no `name:` override on
    the volume, docker-compose auto-prefixes with the project name — those
    are already per-project-scoped and we don't touch them."""
    try:
        with open(compose_path) as f:
            compose = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"0001: skipping {compose_path}: {e}")
        return []
    declared = compose.get("volumes") or {}
    if not isinstance(declared, dict):
        return []
    pairs = []
    for declared_name, spec in declared.items():
        effective = declared_name
        if isinstance(spec, dict) and "name" in spec:
            effective = spec["name"]
        pairs.append((declared_name, effective))
    return pairs


@register
class NamespaceCatalogVolumes(Migration):
    id = "0001_namespace_catalog_volumes"
    description = (
        "Copy pre-fix shared docker volumes (db_data, nextcloud_data, …) into "
        "their new per-instance <uuid>_<name> counterparts so existing "
        "greffons survive the volume-namespacing change."
    )
    stop_on_failure = False

    def run(self, data_root: str) -> dict:
        summary = {"migrated": 0, "skipped": 0, "errors": 0}
        if not os.path.isdir(data_root):
            logger.info(f"0001: data root {data_root} does not exist; skipping")
            return summary

        for instance_id in sorted(os.listdir(data_root)):
            instance_dir = os.path.join(data_root, instance_id)
            compose_path = os.path.join(instance_dir, "docker-compose.yml")
            if not os.path.isfile(compose_path):
                continue
            if instance_id.startswith(".") or "/" in instance_id:
                continue

            for declared, effective in _volumes_declared_in_compose(compose_path):
                prefix = f"{instance_id}_"
                # Already prefixed by this instance (handles greffon_nginx
                # which was always namespaced + already-migrated catalog vols).
                if effective.startswith(prefix) or declared.startswith(prefix):
                    summary["skipped"] += 1
                    continue
                expected = f"{instance_id}_{declared}"
                if effective == expected:
                    summary["skipped"] += 1
                    continue
                if not _volume_exists(effective):
                    # Old volume was never created (compose never booted) —
                    # next start will create the new one empty; nothing to copy.
                    summary["skipped"] += 1
                    continue
                if _volume_exists(expected):
                    # Target already exists (partial retry) — don't touch either side.
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
                        f"0001: {instance_id}/{declared} copied {effective} -> {expected}"
                    )
                except subprocess.CalledProcessError as e:
                    summary["errors"] += 1
                    logger.error(
                        f"0001: failed {instance_id}/{declared}: "
                        f"{e.stderr.decode(errors='replace') if e.stderr else e}"
                    )
        return summary
