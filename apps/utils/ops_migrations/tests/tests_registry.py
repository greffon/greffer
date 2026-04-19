"""Tests for the Migration registry — id validation + dedup."""
from django.test import TestCase

from apps.utils.ops_migrations import registry
from apps.utils.ops_migrations.base import (
    DuplicateMigrationId,
    InvalidMigrationId,
    Migration,
)


class RegistryTests(TestCase):
    def setUp(self):
        self._snapshot = dict(registry._REGISTRY)
        registry.reset_for_tests()

    def tearDown(self):
        registry.reset_for_tests()
        registry._REGISTRY.update(self._snapshot)

    def test_register_adds_class(self):
        class Mig(Migration):
            id = "0042_example_test"
            def run(self, data_root): return {}
        registry.register(Mig)
        ids = [m.id for m in registry.all_migrations()]
        self.assertIn("0042_example_test", ids)

    def test_register_is_sorted(self):
        class A(Migration):
            id = "0003_a_mig"
            def run(self, d): return {}
        class B(Migration):
            id = "0001_b_mig"
            def run(self, d): return {}
        class C(Migration):
            id = "0002_c_mig"
            def run(self, d): return {}
        # Registration order != execution order.
        registry.register(A); registry.register(B); registry.register(C)
        ids = [m.id for m in registry.all_migrations()]
        # Restrict to just the three we added (all_migrations also loads real migs).
        ours = [i for i in ids if i in {A.id, B.id, C.id}]
        self.assertEqual(ours, ["0001_b_mig", "0002_c_mig", "0003_a_mig"])

    def test_duplicate_id_raises(self):
        class Mig1(Migration):
            id = "0099_same_id_test"
            def run(self, d): return {}
        class Mig2(Migration):
            id = "0099_same_id_test"
            def run(self, d): return {}
        registry.register(Mig1)
        with self.assertRaises(DuplicateMigrationId):
            registry.register(Mig2)

    def test_malformed_id_rejected_at_class_definition(self):
        with self.assertRaises(InvalidMigrationId):
            class Mig(Migration):
                id = "bad id — no digits up front"
                def run(self, d): return {}

    def test_register_rejects_non_migration_subclass(self):
        class NotAMigration:
            id = "0001_nope_test"
        with self.assertRaises(TypeError):
            registry.register(NotAMigration)

    def test_register_rejects_empty_id(self):
        class NoId(Migration):
            def run(self, d): return {}
        with self.assertRaises(ValueError):
            registry.register(NoId)
