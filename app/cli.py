"""Greffer operational CLI — replaces Django's ``manage.py`` entrypoint.

Invoked from the container at boot, before uvicorn binds::

    poetry run python -m app.cli apply_ops_migrations

Keeps the exact flag set and exit codes the Django management command
exposed at ``apps/controller/management/commands/apply_ops_migrations.py``
so operator runbooks (``--dry-run``, ``--only``, ``--fail-fast``,
``--restore``) continue to work verbatim.

Exit codes:
    0  — every attempted migration succeeded (or all were already applied).
         ALSO returned when an ``advisory`` migration failed: such a migration
         is best-effort cleanup and must never stop the server from starting,
         so its failure surfaces as a WARN line on stderr plus a CRITICAL log,
         not a non-zero exit. A script that must react to it should match the
         WARN line / the ``advisory_failed`` summary key, not ``$?``.
    1  — at least one NON-advisory migration failed
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

    r = sub.add_parser(
        "renew_certs",
        help=(
            "Renew per-instance upstream certificates now, instead of waiting "
            "for the next worker tick."
        ),
        description=(
            "Runs one renewal pass over this node's running instances: mint a "
            "replacement from the manager, install it into the instance's nginx "
            "sidecar, reload, then verify over TLS that the sidecar is actually "
            "serving the new serial (restarting it if not). Exit code is the "
            "number of instances that errored."
        ),
    )
    r.add_argument(
        "--instance",
        metavar="GREFFON_ID",
        default=None,
        help="Renew exactly one instance (default: every running instance).",
    )
    r.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore this node's backoff and not-due check. For an instance that "
            "is already serving an expired certificate and cannot wait for the "
            "backoff to elapse. The manager still applies its own window, "
            "cooldown and rate limit, so this grants nothing extra."
        ),
    )
    r.set_defaults(func=_renew_certs)

    args = parser.parse_args(argv)
    return args.func(args)


def _renew_certs(args: argparse.Namespace) -> int:
    """Force a renewal pass. Exit code = number of instances that errored.

    Exists for the operator whose instances are ALREADY 502ing on an expired
    certificate: the worker would fix them on its own schedule, but "wait up to
    six hours" is not an answer while an app is down.
    """
    from app.workers.cert_renewal import renew_all

    settings = get_settings()
    return renew_all(settings, only=args.instance, force=args.force)


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
        if r.ok and r.summary.get("advisory_failed"):
            # ok=True only because the migration is advisory (it must not gate
            # boot). A bare OK here would tell an operator the cleanup worked
            # while, for 0002, TLS private keys are still on disk -- and this
            # is the only stdout/stderr signal they get.
            print(
                f"  WARN {r.id} ({r.duration_seconds}s) — advisory migration "
                f"FAILED, boot continues, retries next start: {r.error} "
                f"{r.summary}",
                file=sys.stderr,
            )
        elif r.ok:
            print(f"  OK   {r.id} ({r.duration_seconds}s) {r.summary}")
        else:
            print(
                f"  FAIL {r.id} ({r.duration_seconds}s) — {r.error}",
                file=sys.stderr,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
