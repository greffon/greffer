"""Runner: apply unapplied migrations in order, record results in the ledger.

Entry points:
    apply_pending(data_root, only=None, dry_run=False, fail_fast=False) -> list[Result]

Called from:
    - the Django management command `apply_ops_migrations`
    - tests

Safety properties:
    - fcntl.flock held across the whole batch so two greffer processes can't race
    - `mark_applied` is the LAST step per migration; mid-run crash leaves the
      ledger untouched so the next invocation retries
    - A migration that raises: Result.ok=False, ledger unchanged, continue to
      the next (unless mig.stop_on_failure or batch fail_fast)
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time

from .base import Result, Migration
from .ledger import Ledger
from .registry import all_migrations

logger = logging.getLogger("greffer.ops_migrations")

LOCK_FILENAME = ".greffer-migrations.lock"


@contextlib.contextmanager
def _runner_lock(data_root: str):
    """Exclusive flock held across the batch. Creates data_root if missing."""
    os.makedirs(data_root, exist_ok=True)
    path = os.path.join(data_root, LOCK_FILENAME)
    with open(path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def apply_pending(
    data_root: str | None = None,
    *,
    only: str | None = None,
    dry_run: bool = False,
    fail_fast: bool = False,
) -> list[Result]:
    data_root = data_root or os.getenv("GREFFON_PATH", "/data")
    if os.getenv("GREFFER_SKIP_OPS_MIGRATIONS"):
        logger.warning(
            "GREFFER_SKIP_OPS_MIGRATIONS is set — skipping all ops migrations. "
            "You must run `python manage.py apply_ops_migrations` manually to recover."
        )
        return []

    with _runner_lock(data_root):
        ledger = Ledger.load(data_root)
        results: list[Result] = []
        for mig in all_migrations():
            if only is not None and mig.id != only:
                continue
            if ledger.is_applied(mig.id):
                logger.debug(f"ops-migration {mig.id}: already applied, skipping")
                continue
            if dry_run:
                logger.info(f"ops-migration {mig.id}: would run — {mig.description}")
                results.append(Result(id=mig.id, ok=True, summary={"dry_run": True}))
                continue

            result = _run_single(mig, data_root, ledger)
            results.append(result)
            if not result.ok and (mig.stop_on_failure or fail_fast):
                logger.error(
                    f"ops-migration {mig.id} failed and "
                    f"{'stop_on_failure' if mig.stop_on_failure else '--fail-fast'} "
                    "is set; halting batch."
                )
                break
        return results


def _run_single(mig: Migration, data_root: str, ledger: Ledger) -> Result:
    logger.info(f"ops-migration {mig.id}: starting — {mig.description}")
    started = time.time()
    try:
        mig.check_preconditions(data_root)
        summary = mig.run(data_root) or {}
    except Exception as e:
        logger.exception(f"ops-migration {mig.id}: FAILED: {e}")
        return Result(
            id=mig.id,
            ok=False,
            error=str(e),
            duration_seconds=round(time.time() - started, 3),
        )
    if not isinstance(summary, dict):
        logger.error(
            f"ops-migration {mig.id}: returned {type(summary).__name__}, "
            "not a dict summary; treating as failure (not marking applied)."
        )
        return Result(
            id=mig.id, ok=False,
            error="migration did not return a dict summary",
            duration_seconds=round(time.time() - started, 3),
        )
    duration = round(time.time() - started, 3)
    backups = list(summary.pop("backups", []) or [])

    # Don't mark applied if the migration reported per-item errors. A migration
    # that copied 3 of 5 volumes and logged 2 errors has NOT succeeded; marking
    # it applied would prevent the retry that fixes the remaining 2 (transient
    # docker failures are the common case). The migration body is responsible
    # for making per-item ops idempotent — next run picks up where this left off.
    error_count = int(summary.get("errors") or 0)
    if error_count > 0:
        logger.error(
            f"ops-migration {mig.id}: completed with {error_count} per-item errors "
            f"in {duration}s — NOT marked applied; will retry next run."
        )
        return Result(
            id=mig.id,
            ok=False,
            summary=summary,
            error=f"{error_count} per-item error(s)",
            duration_seconds=duration,
        )

    ledger.mark_applied(
        mig.id,
        summary=summary,
        duration_seconds=duration,
        backups=backups,
    )
    logger.info(f"ops-migration {mig.id}: applied in {duration}s — {summary}")
    return Result(id=mig.id, ok=True, summary=summary, duration_seconds=duration)
