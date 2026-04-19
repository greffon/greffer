"""Tests for runner.apply_pending — ordering, idempotence, failure semantics."""
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from apps.utils.ops_migrations import registry, runner
from apps.utils.ops_migrations.base import Migration
from apps.utils.ops_migrations.ledger import Ledger


def _with_fresh_registry(fn):
    """Decorator — snapshot the registry before, restore after. Lets tests
    define their own Migration classes without polluting real migrations."""
    def wrapper(self, *a, **kw):
        snapshot = dict(registry._REGISTRY)
        registry.reset_for_tests()
        try:
            return fn(self, *a, **kw)
        finally:
            registry.reset_for_tests()
            registry._REGISTRY.update(snapshot)
    return wrapper


class RunnerTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    @_with_fresh_registry
    def test_runs_migrations_in_id_order(self):
        calls = []

        class Second(Migration):
            id = "0002_second_test"
            def run(self, data_root):
                calls.append(self.id); return {}

        class First(Migration):
            id = "0001_first_test"
            def run(self, data_root):
                calls.append(self.id); return {}

        # Register out of order — runner must still run 0001 before 0002.
        registry.register(Second)
        registry.register(First)

        results = runner.apply_pending(data_root=self.tmp)
        self.assertEqual(calls, ["0001_first_test", "0002_second_test"])
        self.assertTrue(all(r.ok for r in results))

    @_with_fresh_registry
    def test_skips_already_applied_migration(self):
        calls = []

        class Mig(Migration):
            id = "0001_already_applied"
            def run(self, data_root):
                calls.append(self.id); return {}

        registry.register(Mig)
        # Pre-mark as applied.
        ledger = Ledger.load(self.tmp)
        ledger.mark_applied("0001_already_applied", {}, 0.0)

        runner.apply_pending(data_root=self.tmp)
        self.assertEqual(calls, [])

    @_with_fresh_registry
    def test_failure_does_not_mark_applied_so_retries_work(self):
        attempts = []

        class Boom(Migration):
            id = "0001_raises"
            def run(self, data_root):
                attempts.append(1)
                raise RuntimeError("nope")

        registry.register(Boom)

        runner.apply_pending(data_root=self.tmp)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(Ledger.load(self.tmp).is_applied("0001_raises"))

        # Second run: migration retries — ledger still unmarked.
        runner.apply_pending(data_root=self.tmp)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(Ledger.load(self.tmp).is_applied("0001_raises"))

    @_with_fresh_registry
    def test_stop_on_failure_halts_batch(self):
        calls = []

        class HardFail(Migration):
            id = "0001_hard_fail"
            stop_on_failure = True
            def run(self, data_root):
                calls.append(self.id); raise RuntimeError("stop")

        class WouldSucceed(Migration):
            id = "0002_would_succeed"
            def run(self, data_root):
                calls.append(self.id); return {}

        registry.register(HardFail)
        registry.register(WouldSucceed)

        results = runner.apply_pending(data_root=self.tmp)
        self.assertEqual(calls, ["0001_hard_fail"])  # 0002 not reached
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)

    @_with_fresh_registry
    def test_fail_fast_halts_batch_even_without_stop_on_failure(self):
        calls = []

        class SoftFail(Migration):
            id = "0001_soft_fail"
            stop_on_failure = False
            def run(self, data_root):
                calls.append(self.id); raise RuntimeError("soft")

        class Would(Migration):
            id = "0002_would_run"
            def run(self, data_root):
                calls.append(self.id); return {}

        registry.register(SoftFail)
        registry.register(Would)

        runner.apply_pending(data_root=self.tmp, fail_fast=True)
        self.assertEqual(calls, ["0001_soft_fail"])

    @_with_fresh_registry
    def test_dry_run_does_not_execute_or_mark(self):
        calls = []

        class Mig(Migration):
            id = "0001_dry_run_test"
            def run(self, data_root):
                calls.append(self.id); return {}

        registry.register(Mig)
        results = runner.apply_pending(data_root=self.tmp, dry_run=True)
        self.assertEqual(calls, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].summary, {"dry_run": True})
        self.assertFalse(Ledger.load(self.tmp).is_applied("0001_dry_run_test"))

    @_with_fresh_registry
    def test_only_runs_exactly_one_migration(self):
        calls = []

        class A(Migration):
            id = "0001_a_test"
            def run(self, d):
                calls.append(self.id); return {}

        class B(Migration):
            id = "0002_b_test"
            def run(self, d):
                calls.append(self.id); return {}

        registry.register(A)
        registry.register(B)
        runner.apply_pending(data_root=self.tmp, only="0002_b_test")
        self.assertEqual(calls, ["0002_b_test"])

    @_with_fresh_registry
    def test_partial_apply_state_not_recorded(self):
        """Migration mutates state THEN raises → ledger entry must be absent."""
        class PartialFail(Migration):
            id = "0001_partial_test"
            def run(self, data_root):
                # Pretend we wrote half a side-effect, then raised.
                with open(os.path.join(data_root, "half_written"), "w") as f:
                    f.write("x")
                raise RuntimeError("boom after mutation")

        registry.register(PartialFail)
        runner.apply_pending(data_root=self.tmp)
        self.assertFalse(Ledger.load(self.tmp).is_applied("0001_partial_test"))
        # Side-effect persisted — that's realistic, migration must be idempotent.
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "half_written")))

    @_with_fresh_registry
    def test_per_item_errors_prevent_marking_applied(self):
        """Regression: a migration that reports `{errors: N}` in its summary
        (instead of raising) must NOT be marked applied — otherwise failed
        per-item ops like transient docker errors never get retried."""
        class PartialErrors(Migration):
            id = "0001_some_errors_test"
            def run(self, data_root):
                return {"migrated": 3, "skipped": 0, "errors": 2}

        registry.register(PartialErrors)
        results = runner.apply_pending(data_root=self.tmp)

        # Runner returns the summary (so operators see what happened), but
        # reports ok=False and does NOT mark the migration applied.
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].summary, {"migrated": 3, "skipped": 0, "errors": 2})
        self.assertFalse(Ledger.load(self.tmp).is_applied("0001_some_errors_test"))

        # Next boot retries. Make it succeed this time.
        registry.reset_for_tests()

        class Retried(Migration):
            id = "0001_some_errors_test"
            def run(self, data_root):
                return {"migrated": 2, "skipped": 3, "errors": 0}

        registry.register(Retried)
        results = runner.apply_pending(data_root=self.tmp)
        self.assertTrue(results[0].ok)
        self.assertTrue(Ledger.load(self.tmp).is_applied("0001_some_errors_test"))

    @_with_fresh_registry
    def test_skip_env_var_bypasses_runner(self):
        calls = []

        class Mig(Migration):
            id = "0001_skipped_test"
            def run(self, d):
                calls.append(self.id); return {}

        registry.register(Mig)
        with patch.dict(os.environ, {"GREFFER_SKIP_OPS_MIGRATIONS": "1"}):
            results = runner.apply_pending(data_root=self.tmp)
        self.assertEqual(calls, [])
        self.assertEqual(results, [])


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
