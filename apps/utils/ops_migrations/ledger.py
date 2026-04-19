"""Atomic JSON ledger of applied migrations.

The ledger lives at `$GREFFON_PATH/.greffer-migrations.json`. Shape:

    {
      "version": 1,
      "applied": [
        {"id": "0001_namespace_catalog_volumes",
         "applied_at": "2026-04-18T09:12:03Z",
         "duration_seconds": 4.21,
         "summary": {...},
         "backups": []}
      ]
    }

Why not a Django model? Migrations may mutate `$GREFFON_PATH` itself,
including possibly destructive docker daemon state; relying on the DB for
bookkeeping adds a second failure mode. A JSON file on the same filesystem
the migrations operate on is simpler and the operator can grep it.

Writes are atomic: serialize → write to `.greffer-migrations.json.tmp` → fsync
→ `os.replace`. Unknown top-level keys are preserved round-trip for future
framework versions.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger("greffer.ops_migrations")

LEDGER_FILENAME = ".greffer-migrations.json"
LEGACY_SENTINEL_FILENAME = ".volumes-migrated"
LEGACY_SENTINEL_MIGRATES = "0001_namespace_catalog_volumes"
CURRENT_VERSION = 1


class Ledger:
    """In-memory view of the JSON ledger, write-through on mutations."""

    def __init__(self, data_root: str, data: dict, path: str):
        self.data_root = data_root
        self._data = data
        self._path = path

    # ---- factories ---------------------------------------------------

    @classmethod
    def load(cls, data_root: str) -> "Ledger":
        """Load from disk, applying the legacy-sentinel shim if present."""
        path = os.path.join(data_root, LEDGER_FILENAME)
        data = cls._read_or_empty(path)
        data = cls._apply_legacy_sentinel_shim(data_root, data, path)
        return cls(data_root, data, path)

    # ---- read ops ----------------------------------------------------

    @property
    def applied_ids(self) -> set[str]:
        return {entry["id"] for entry in self._data.get("applied", [])}

    def is_applied(self, migration_id: str) -> bool:
        return migration_id in self.applied_ids

    @property
    def applied(self) -> list[dict]:
        # Copy so callers can't mutate our state.
        return [dict(e) for e in self._data.get("applied", [])]

    # ---- mutate ------------------------------------------------------

    def mark_applied(
        self,
        migration_id: str,
        summary: dict,
        duration_seconds: float,
        backups: list[str] | None = None,
    ) -> None:
        if self.is_applied(migration_id):
            return
        entry = {
            "id": migration_id,
            "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(float(duration_seconds), 3),
            "summary": dict(summary or {}),
            "backups": list(backups or []),
        }
        self._data.setdefault("applied", []).append(entry)
        self._write()

    # ---- internals ---------------------------------------------------

    @staticmethod
    def _read_or_empty(path: str) -> dict:
        if not os.path.isfile(path):
            return {"version": CURRENT_VERSION, "applied": []}
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # Loud + refuse to auto-write so an operator sees it rather than
            # silently re-running every past migration.
            logger.error(
                f"ledger at {path} is unreadable ({e}); returning empty view. "
                "Use --force-rebuild-ledger if you know the on-disk state "
                "reflects successful past migrations."
            )
            return {"version": CURRENT_VERSION, "applied": [], "_read_error": str(e)}
        if not isinstance(data, dict):
            logger.error(f"ledger at {path} is not a JSON object; returning empty view.")
            return {"version": CURRENT_VERSION, "applied": [], "_read_error": "not-an-object"}
        data.setdefault("version", CURRENT_VERSION)
        data.setdefault("applied", [])
        return data

    @classmethod
    def _apply_legacy_sentinel_shim(cls, data_root: str, data: dict, ledger_path: str) -> dict:
        """If an old-world sentinel file is present AND the ledger doesn't
        already record that migration, seed the ledger with it and unlink
        the sentinel. One-shot transition for PR #10 → framework."""
        sentinel = os.path.join(data_root, LEGACY_SENTINEL_FILENAME)
        if not os.path.exists(sentinel):
            return data
        applied_ids = {entry.get("id") for entry in data.get("applied", [])}
        if LEGACY_SENTINEL_MIGRATES in applied_ids:
            # Ledger already knows — just remove the sentinel.
            try:
                os.unlink(sentinel)
            except OSError:
                pass
            return data
        logger.info(
            f"ops-migrations: found legacy sentinel {sentinel}; "
            f"seeding ledger with {LEGACY_SENTINEL_MIGRATES} as applied."
        )
        entry = {
            "id": LEGACY_SENTINEL_MIGRATES,
            "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": 0.0,
            "summary": {"source": "legacy-sentinel-shim"},
            "backups": [],
        }
        data.setdefault("applied", []).append(entry)
        # Persist before unlinking so we never lose the bit.
        cls._atomic_write(ledger_path, data)
        try:
            os.unlink(sentinel)
        except OSError as e:
            logger.warning(f"ops-migrations: could not remove legacy sentinel: {e}")
        return data

    def _write(self) -> None:
        self._atomic_write(self._path, self._data)

    @staticmethod
    def _atomic_write(path: str, data: dict) -> None:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        # Write to a sibling temp file on the same filesystem so os.replace
        # is atomic (same-fs rename == single inode swap).
        fd, tmp = tempfile.mkstemp(prefix=".greffer-mig-", dir=parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
