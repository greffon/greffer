"""Every registered migration must be invocable THROUGH THE RUNNER.

This exists because 0002 shipped with `def run(self)` while the runner calls
`mig.run(data_root)`. Its five unit tests all called `.run()` directly, so they
passed while the migration raised TypeError on every real boot — and the boot
command ``&&``-gates uvicorn on the migration step, so the whole fleet would
have crash-looped on upgrade.

Testing each migration's `run()` in isolation cannot catch a signature that
disagrees with its caller. This drives the actual entrypoint instead, and it is
generic so a future migration cannot reintroduce the same mistake.
"""
import inspect

from apps.utils.ops_migrations import runner
from apps.utils.ops_migrations.base import Migration
from apps.utils.ops_migrations.registry import all_migrations


def test_every_migration_matches_the_runner_signature():
    expected = inspect.signature(Migration.run).parameters.keys()
    for mig in all_migrations():
        actual = inspect.signature(type(mig).run).parameters.keys()
        assert list(actual) == list(expected), (
            f'{mig.id}.run{tuple(actual)} does not match the runner contract '
            f'{tuple(expected)} — the runner calls mig.run(data_root), so this '
            f'raises TypeError at boot and uvicorn never starts'
        )


def test_every_migration_runs_clean_through_the_runner(tmp_path, monkeypatch):
    """Drive `apply_pending` for real against an empty data root. A migration
    that cannot even be *invoked* must fail here, not in production."""
    monkeypatch.chdir(tmp_path)                      # nothing to act on
    monkeypatch.delenv('GREFFER_SKIP_OPS_MIGRATIONS', raising=False)
    data_root = tmp_path / 'data'
    data_root.mkdir()

    for mig in all_migrations():
        results = runner.apply_pending(str(data_root), only=mig.id)
        assert results, f'{mig.id}: runner returned no result'
        for res in results:
            assert res.ok, (
                f'{mig.id} failed through the runner on an empty data root: '
                f'{getattr(res, "error", None)}'
            )
