"""Run pending greffer ops-migrations.

Usage:
    python manage.py apply_ops_migrations                # apply all pending
    python manage.py apply_ops_migrations --dry-run      # list pending, no writes
    python manage.py apply_ops_migrations --only 0001_…  # apply exactly one
    python manage.py apply_ops_migrations --fail-fast    # halt on first failure
    python manage.py apply_ops_migrations --restore 0001_…  # print backups paths

Exit codes:
    0  — every attempted migration succeeded (or all were already applied)
    1  — at least one migration failed
    2  — bad arguments (e.g. --only references an unknown id)
"""
from __future__ import annotations

import os
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.utils.ops_migrations import operations, runner
from apps.utils.ops_migrations.registry import all_migrations


class Command(BaseCommand):
    help = "Apply pending greffer operational migrations (on-disk + docker state)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List migrations that would be applied without touching state.",
        )
        parser.add_argument(
            "--only", metavar="MIGRATION_ID", default=None,
            help="Apply exactly one migration by id (must match registered id).",
        )
        parser.add_argument(
            "--fail-fast", action="store_true",
            help="Halt the batch on the first failure (default: keep going unless "
                 "the migration declares stop_on_failure=True).",
        )
        parser.add_argument(
            "--restore", metavar="MIGRATION_ID", default=None,
            help="Print the backup paths recorded for the given migration. "
                 "Manual step after that: apply them yourself.",
        )
        parser.add_argument(
            "--data-root", default=None,
            help="Override $GREFFON_PATH (default: env var or /data).",
        )

    def handle(self, *args, **opts):
        data_root = opts["data_root"] or os.getenv("GREFFON_PATH", "/data")

        if opts["restore"]:
            paths = operations.restore(opts["restore"], data_root)
            if not paths:
                self.stdout.write(self.style.WARNING(
                    f"no backups recorded for {opts['restore']}"
                ))
                return
            self.stdout.write(f"backups for {opts['restore']}:")
            for p in paths:
                self.stdout.write(f"  {p}")
            return

        if opts["only"]:
            known_ids = {m.id for m in all_migrations()}
            if opts["only"] not in known_ids:
                raise CommandError(
                    f"--only {opts['only']!r}: no migration with that id registered. "
                    f"Known: {sorted(known_ids)}"
                )

        results = runner.apply_pending(
            data_root=data_root,
            only=opts["only"],
            dry_run=opts["dry_run"],
            fail_fast=opts["fail_fast"],
        )

        if not results:
            self.stdout.write("no pending migrations")
            return

        failures = [r for r in results if not r.ok]
        for r in results:
            if r.ok:
                line = f"  OK   {r.id} ({r.duration_seconds}s) {r.summary}"
                self.stdout.write(self.style.SUCCESS(line))
            else:
                line = f"  FAIL {r.id} ({r.duration_seconds}s) — {r.error}"
                self.stdout.write(self.style.ERROR(line))

        if failures:
            sys.exit(1)
