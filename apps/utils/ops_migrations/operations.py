"""Optional snapshot/restore helpers migrations can opt into.

Any migration that mutates host state in place (as opposed to 0001, which
only copies), MUST call `snapshot_volume` or `snapshot_file` first and
return the resulting paths in the `backups` key of its summary dict.

Backups land at `$GREFFON_PATH/.migration-backups/<migration-id>/<item>`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger("greffer.ops_migrations")

BACKUPS_DIR = ".migration-backups"


def _backups_dir(data_root: str, migration_id: str) -> str:
    path = os.path.join(data_root, BACKUPS_DIR, migration_id)
    os.makedirs(path, exist_ok=True)
    return path


def snapshot_volume(volume_name: str, migration_id: str, data_root: str) -> str:
    """Tar the contents of `volume_name` into `<backups>/<volume_name>.tar.gz`.

    Uses a short-lived alpine container to stream the tar out so ownership
    and permissions are preserved. Atomic write: streams to a tmp file then
    renames. Returns the final path of the archive.
    """
    out_dir = _backups_dir(data_root, migration_id)
    final = os.path.join(out_dir, f"{volume_name}.tar.gz")
    if os.path.exists(final):
        logger.debug(f"snapshot_volume: {final} already exists, skipping")
        return final
    fd, tmp = tempfile.mkstemp(prefix=".snap-", suffix=".tar.gz", dir=out_dir)
    os.close(fd)
    try:
        with open(tmp, "wb") as sink:
            proc = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{volume_name}:/vol:ro",
                    "alpine:3.20",
                    "sh", "-c", "cd /vol && tar czf - .",
                ],
                stdout=sink, stderr=subprocess.PIPE, check=True,
            )
        os.replace(tmp, final)
        logger.info(f"snapshot_volume: {volume_name} -> {final}")
        return final
    except subprocess.CalledProcessError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError(
            f"snapshot_volume({volume_name!r}) failed: {e.stderr.decode(errors='replace')}"
        ) from e


def snapshot_file(file_path: str, migration_id: str, data_root: str) -> str | None:
    """Copy `file_path` into the migration's backup directory.

    Returns the final backup path, or None if the source doesn't exist.
    """
    if not os.path.exists(file_path):
        logger.debug(f"snapshot_file: {file_path} does not exist, skipping")
        return None
    out_dir = _backups_dir(data_root, migration_id)
    final = os.path.join(out_dir, os.path.basename(file_path))
    shutil.copy2(file_path, final)
    logger.info(f"snapshot_file: {file_path} -> {final}")
    return final


def restore(migration_id: str, data_root: str) -> list[str]:
    """Return the backup paths recorded for this migration, in order. Callers
    (typically the `--restore` flag in the management command) decide how
    to use each one — we don't know without per-migration context whether
    to `docker cp` back into a volume or overwrite a file."""
    path = os.path.join(data_root, BACKUPS_DIR, migration_id)
    if not os.path.isdir(path):
        return []
    return sorted(os.path.join(path, f) for f in os.listdir(path))
