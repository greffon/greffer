"""One-time migration: rename un-prefixed docker volumes from pre-namespacing
greffons to the new `<instance_id>_<name>` scheme.

Before the volume-namespacing fix, the greffer wrote catalog-declared volumes
to docker under their raw compose-author labels (e.g. `db_data`). Multiple
instances thus shared the same host volume, causing silent data corruption.

After the fix, each instance's volumes are namespaced by its greffon_info id.
Existing instances deployed under the old scheme still have their data in the
old shared volumes — they need a one-time copy into the new per-instance
volume names, or their next restart will come up with empty volumes and look
like data loss.

This module scans the greffer's data root (one subdir per instance, each with
its rendered docker-compose.yml), identifies volumes that still use the raw
label, and for each:

  1. Creates a new `<instance_id>_<name>` docker volume.
  2. Copies the contents of the shared volume into the new one via a
     short-lived alpine container (tar stream, preserves ownership + times).
  3. Leaves the shared volume in place (never destructive — operator can
     prune it once they've verified the migration).

Idempotent: writes a sentinel file `<data_root>/.volumes-migrated` after a
successful pass so subsequent greffer starts are no-ops.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Iterable

import yaml
from django.conf import settings

logger = logging.getLogger(settings.LOGGER_NAME)

SENTINEL_NAME = ".volumes-migrated"


def _volume_exists(name: str) -> bool:
    """True iff a docker volume with this exact name is currently defined."""
    res = subprocess.run(
        ["docker", "volume", "inspect", name],
        capture_output=True,
    )
    return res.returncode == 0


def _copy_volume(src: str, dst: str) -> None:
    """Copy every file from volume `src` to volume `dst` via an alpine
    container, preserving ownership/permissions. Raises on non-zero exit."""
    logger.info(f"volume migration: copying {src} -> {dst}")
    # -C /from + `.` captures hidden files too; target volume must exist first.
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
    top-level volume declared in compose_path. `effective_name_in_rendered_compose`
    is whatever the `name:` override was set to — that's the string the
    pre-migration greffer wrote and what the real docker volume is called.
    If no `name:` override, docker-compose auto-prefixes; we don't touch those
    (they're already per-project-scoped)."""
    try:
        with open(compose_path) as f:
            compose = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"volume migration: skipping {compose_path}: {e}")
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


def run(data_root: str | None = None) -> dict:
    """Scan `data_root` for instance dirs and migrate any un-namespaced volumes.

    Returns a summary dict with `migrated`, `skipped`, `errors` counts so
    callers/tests can assert behavior. Writes a sentinel file on success so
    subsequent runs are no-ops.
    """
    data_root = data_root or os.getenv("GREFFON_PATH", "/data")
    summary = {"migrated": 0, "skipped": 0, "errors": 0}

    if not os.path.isdir(data_root):
        logger.info(f"volume migration: data root {data_root} does not exist; skipping")
        return summary

    sentinel = os.path.join(data_root, SENTINEL_NAME)
    if os.path.exists(sentinel):
        return summary

    for instance_id in sorted(os.listdir(data_root)):
        instance_dir = os.path.join(data_root, instance_id)
        compose_path = os.path.join(instance_dir, "docker-compose.yml")
        if not os.path.isfile(compose_path):
            continue
        # Skip things that don't look like a greffon instance ID.
        if instance_id.startswith(".") or "/" in instance_id:
            continue

        for declared, effective in _volumes_declared_in_compose(compose_path):
            # Already prefixed by this instance (handles both `greffon_nginx`
            # which was always namespaced and already-migrated catalog volumes).
            prefix = f"{instance_id}_"
            if effective.startswith(prefix) or declared.startswith(prefix):
                summary["skipped"] += 1
                continue
            expected = f"{instance_id}_{declared}"
            if effective == expected:
                # Already namespaced — nothing to do.
                summary["skipped"] += 1
                continue
            if not _volume_exists(effective):
                # Old volume was never created (compose never booted) — nothing
                # to copy; next start will just create the new one empty.
                summary["skipped"] += 1
                continue
            if _volume_exists(expected):
                # Already migrated OR name conflict — don't touch either side.
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
                    f"volume migration: {instance_id}/{declared} "
                    f"copied from {effective} -> {expected}"
                )
            except subprocess.CalledProcessError as e:
                summary["errors"] += 1
                logger.error(
                    f"volume migration: failed {instance_id}/{declared}: "
                    f"{e.stderr.decode(errors='replace') if e.stderr else e}"
                )

    # Only drop the sentinel if nothing errored — otherwise next boot retries
    # the partial migration.
    if summary["errors"] == 0:
        try:
            with open(sentinel, "w") as f:
                f.write(
                    "greffer volume namespacing migration complete. "
                    "Old shared volumes (db_data, nextcloud_data, etc.) are "
                    "left in place; `docker volume prune` once you've "
                    "verified each greffon comes up cleanly.\n"
                )
        except OSError as e:
            logger.warning(f"volume migration: could not write sentinel: {e}")

    return summary
