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
            "You must run `python -m app.cli apply_ops_migrations` manually to recover."
        )
        return []

    with _runner_lock(data_root):
        ledger = Ledger.load(data_root)
        results: list[Result] = []
        for mig in all_migrations():
            if only is not None and mig.id != only:
                continue
            # Advisory migrations deliberately ignore the ledger: their targets
            # can appear after the first run, so a one-shot purge would skip
            # exactly the state they exist to remove. See Migration.advisory.
            if ledger.is_applied(mig.id) and not mig.advisory:
                logger.debug(f"ops-migration {mig.id}: already applied, skipping")
                continue
            if dry_run:
                logger.info(f"ops-migration {mig.id}: would run — {mig.description}")
                results.append(Result(id=mig.id, ok=True, summary={"dry_run": True}))
                continue

            result = _run_single(mig, data_root, ledger)
            # Batch control must see the REAL outcome. `advisory` governs whether
            # a failure gates BOOT (the process exit code), NOT whether an
            # operator's explicit --fail-fast / stop_on_failure halt request is
            # honoured -- reading the downgraded result below would ignore both.
            # Safe at boot: the Dockerfile CMD passes neither flag.
            really_failed = not result.ok
            if mig.advisory and really_failed:
                # Best-effort cleanup must not brick the node. Downgrade to a
                # non-gating result so `app.cli` exits 0 and uvicorn still
                # binds -- but log CRITICAL, because for 0002 this means key
                # material is STILL on disk and an operator has to act. The
                # next boot retries (advisory ignores the ledger).
                logger.critical(
                    f"ops-migration {mig.id}: FAILED but is advisory — boot "
                    f"continues and it will retry next start. Error: "
                    f"{result.error}. Summary: {result.summary}"
                )
                result = Result(
                    id=result.id,
                    ok=True,
                    summary={**result.summary, "advisory_failed": True},
                    error=result.error,
                    duration_seconds=result.duration_seconds,
                )
            results.append(result)
            if really_failed and (mig.stop_on_failure or fail_fast):
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
    # Defensive, same reason as the `errors` guard below: a misbehaving
    # migration returning a non-iterable `backups` raised TypeError straight
    # out of apply_pending, bypassing even the advisory downgrade.
    try:
        backups = list(summary.pop("backups", []) or [])
    except TypeError:
        logger.error(
            f"ops-migration {mig.id}: returned non-iterable 'backups' key; "
            "treating as failure (not marking applied)."
        )
        return Result(
            id=mig.id,
            ok=False,
            summary=summary,
            error="malformed summary.backups",
            duration_seconds=duration,
        )

    # Don't mark applied if the migration reported per-item errors. A migration
    # that copied 3 of 5 volumes and logged 2 errors has NOT succeeded; marking
    # it applied would prevent the retry that fixes the remaining 2 (transient
    # docker failures are the common case). The migration body is responsible
    # for making per-item ops idempotent — next run picks up where this left off.
    #
    # Defensive: a malformed summary (non-numeric `errors`) from a misbehaving
    # migration becomes a normal failure result instead of crashing the batch.
    try:
        error_count = int(summary.get("errors") or 0)
    except (TypeError, ValueError):
        logger.error(
            f"ops-migration {mig.id}: returned non-numeric 'errors' key "
            f"({summary.get('errors')!r}); treating as failure (not marking applied)."
        )
        return Result(
            id=mig.id, ok=False, summary=summary,
            error=f"malformed summary.errors={summary.get('errors')!r}",
            duration_seconds=duration,
        )
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

    if mig.advisory:
        # Advisory migrations re-run every boot, so the read side above ignores
        # their ledger entry -- it has no consumer. Writing it anyway was a live
        # crashloop risk: `mark_applied` is OUTSIDE the try above, so an
        # ENOSPC/EIO inside Ledger._atomic_write propagates out of apply_pending,
        # exits non-zero, and the `&&`-gated CMD never starts uvicorn -- the very
        # failure this flag exists to prevent, reached by another route. It also
        # froze run 1's summary forever, since mark_applied early-returns once
        # applied while later runs reported real failures.
        logger.info(
            f"ops-migration {mig.id}: advisory, completed in {duration}s — "
            f"{summary} (not recorded in the ledger; re-runs every boot)"
        )
        return Result(
            id=mig.id, ok=True, summary=summary, duration_seconds=duration)

    ledger.mark_applied(
        mig.id,
        summary=summary,
        duration_seconds=duration,
        backups=backups,
    )
    logger.info(f"ops-migration {mig.id}: applied in {duration}s — {summary}")
    return Result(id=mig.id, ok=True, summary=summary, duration_seconds=duration)
