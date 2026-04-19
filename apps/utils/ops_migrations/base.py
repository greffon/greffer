"""Base types for the greffer operational migration framework.

Each breaking change to greffer's on-disk / docker-managed state (volumes,
$GREFFON_PATH layout, nginx config, cert store, etc.) is expressed as a
`Migration` subclass with a unique sortable ID. The runner applies unapplied
migrations in order and records results in a JSON ledger.

Author rules — *required* for every migration we add:

1. Idempotent per item. Mid-run crash + retry must leave the system in the
   same state it would have been in if the migration had completed cleanly
   on the first try.
2. Non-destructive to old state by default. If a migration moves data from
   location A to location B, it must leave A in place and let the operator
   prune it separately. Destructive ops MUST call
   `ops_migrations.operations.snapshot_*` first and record the backup path
   in the returned summary.
3. `run()` returns a JSON-serializable dict summary. Keys that downstream
   code / operators look at:
     - "migrated": int — items actually touched this run
     - "skipped":  int — items that were already in the target state
     - "errors":   int — items that failed (migration overall may still
                   complete successfully; summary is the record)
     - "backups":  list[str] — filesystem paths the operator can restore from
   Additional migration-specific keys are fine.
4. Any state that would be pointless to migrate if the greffer never booted
   under the old code (e.g. the migration only matters if old-style volumes
   exist) should check and early-return from `run()` with all-zero summary.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

ID_RE = re.compile(r"^\d{4}_[a-z][a-z0-9_]*$")


class MigrationError(Exception):
    """Base class for migration-framework exceptions."""


class InvalidMigrationId(MigrationError):
    """Raised if a migration's ID doesn't match the NNNN_snake_case convention."""


class DuplicateMigrationId(MigrationError):
    """Raised if two migrations register with the same id."""


@dataclass
class Result:
    """What the runner records for each migration in a batch."""
    id: str
    ok: bool
    summary: dict = field(default_factory=dict)
    error: str | None = None
    duration_seconds: float = 0.0


class Migration(ABC):
    """Subclass this and register via `ops_migrations.registry.register`."""

    # Unique, sortable. Must match r"^\d{4}_[a-z][a-z0-9_]*$".
    id: str = ""

    # Short human description. Printed by `--dry-run`.
    description: str = ""

    # If True, a raise in this migration halts the rest of the batch.
    # Default False: log the error, mark this migration as not-applied, and
    # keep going. Set True for anything whose failure would corrupt later
    # migrations that depend on the before-state.
    stop_on_failure: bool = False

    @abstractmethod
    def run(self, data_root: str) -> dict:
        """Perform the migration. Return a JSON-serializable summary dict.

        MUST be idempotent per item. MUST not mark itself applied — the
        runner does that on clean return. Raise any exception to signal
        failure; the runner records it and decides continue vs. halt.
        """

    def check_preconditions(self, data_root: str) -> None:
        """Optional hook run before `run`. Raise to abort this migration
        (runner records error, does not mark applied). Default no-op."""
        return

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # ABC subclasses that don't yet set an id stay abstract — enforce
        # the format only on concrete classes (those without `abstract=True`
        # kwarg *and* with a non-empty id string).
        if cls.id and not ID_RE.match(cls.id):
            raise InvalidMigrationId(
                f"{cls.__name__}.id={cls.id!r} must match {ID_RE.pattern!r} "
                "(e.g. '0001_namespace_catalog_volumes')"
            )
