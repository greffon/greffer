"""Greffer operational CLI — replaces Django's ``manage.py`` entrypoint.

Invoked from the container at boot, before uvicorn binds::

    poetry run python -m app.cli apply_ops_migrations

Keeps the exact flag set and exit codes the Django management command
exposed at ``apps/controller/management/commands/apply_ops_migrations.py``
so operator runbooks (``--dry-run``, ``--only``, ``--fail-fast``,
``--restore``) continue to work verbatim.

Exit codes:
    0  — every attempted migration succeeded (or all were already applied)
    1  — at least one migration failed
    2  — bad arguments (e.g. --only references an unknown id)
"""
from __future__ import annotations

import argparse
import sys

from app.settings import get_settings
from apps.utils.ops_migrations import operations, runner
from apps.utils.ops_migrations.registry import all_migrations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Greffer operational CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser(
        "apply_ops_migrations",
        help="Apply pending greffer operational migrations (on-disk + docker state).",
    )
    m.add_argument(
        "--dry-run",
        action="store_true",
        help="List migrations that would be applied without touching state.",
    )
    m.add_argument(
        "--only",
        metavar="MIGRATION_ID",
        default=None,
        help="Apply exactly one migration by id (must match a registered id).",
    )
    m.add_argument(
        "--fail-fast",
        action="store_true",
        help=(
            "Halt the batch on the first failure (default: keep going unless "
            "the migration declares stop_on_failure=True)."
        ),
    )
    m.add_argument(
        "--restore",
        metavar="MIGRATION_ID",
        default=None,
        help=(
            "Print the backup paths recorded for the given migration. "
            "Manual step after that: apply them yourself."
        ),
    )
    m.add_argument(
        "--data-root",
        default=None,
        help="Override $GREFFON_PATH (default: value from Settings).",
    )
    m.set_defaults(func=_apply_ops_migrations)

    args = parser.parse_args(argv)
    return args.func(args)


def _apply_ops_migrations(args: argparse.Namespace) -> int:
    # Force the settings singleton to hydrate so a missing $GREFFER_ID (etc.)
    # crashes here, not silently below. Override data-root if the CLI flag
    # was passed.
    settings = get_settings()
    data_root = args.data_root or str(settings.greffon_path)

    if args.restore:
        paths = operations.restore(args.restore, data_root)
        if not paths:
            print(f"no backups recorded for {args.restore}", file=sys.stderr)
            return 0
        print(f"backups for {args.restore}:")
        for p in paths:
            print(f"  {p}")
        return 0

    if args.only:
        known_ids = {m.id for m in all_migrations()}
        if args.only not in known_ids:
            print(
                f"--only {args.only!r}: no migration with that id registered. "
                f"Known: {sorted(known_ids)}",
                file=sys.stderr,
            )
            return 2

    results = runner.apply_pending(
        data_root=data_root,
        only=args.only,
        dry_run=args.dry_run,
        fail_fast=args.fail_fast,
    )

    if not results:
        print("no pending migrations")
        return 0

    failures = [r for r in results if not r.ok]
    for r in results:
        if r.ok:
            print(f"  OK   {r.id} ({r.duration_seconds}s) {r.summary}")
        else:
            print(
                f"  FAIL {r.id} ({r.duration_seconds}s) — {r.error}",
                file=sys.stderr,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
