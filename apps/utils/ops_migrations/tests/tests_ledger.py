"""Tests for Ledger — persistence + legacy sentinel shim."""
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from apps.utils.ops_migrations.ledger import (
    Ledger,
    LEDGER_FILENAME,
    LEGACY_SENTINEL_FILENAME,
    LEGACY_SENTINEL_MIGRATES,
)


class LedgerLoadTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def test_missing_file_returns_empty_ledger(self):
        ledger = Ledger.load(self.tmp)
        self.assertEqual(ledger.applied_ids, set())

    def test_corrupt_file_does_not_crash_or_auto_write(self):
        path = os.path.join(self.tmp, LEDGER_FILENAME)
        with open(path, "w") as f:
            f.write("not json")
        orig_mtime = os.stat(path).st_mtime
        ledger = Ledger.load(self.tmp)
        self.assertEqual(ledger.applied_ids, set())
        self.assertEqual(os.stat(path).st_mtime, orig_mtime)

    def test_round_trips_unknown_keys(self):
        path = os.path.join(self.tmp, LEDGER_FILENAME)
        with open(path, "w") as f:
            json.dump({"version": 1, "applied": [], "future_feature": "keep-me"}, f)
        ledger = Ledger.load(self.tmp)
        ledger.mark_applied("0001_something_else", {"x": 1}, 0.1)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["future_feature"], "keep-me")
        self.assertEqual(len(data["applied"]), 1)


class LedgerMarkAppliedTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def test_mark_applied_persists(self):
        ledger = Ledger.load(self.tmp)
        ledger.mark_applied("0001_abc", {"migrated": 3}, 1.23, backups=["/a", "/b"])

        fresh = Ledger.load(self.tmp)
        self.assertTrue(fresh.is_applied("0001_abc"))
        entry = fresh.applied[0]
        self.assertEqual(entry["id"], "0001_abc")
        self.assertEqual(entry["summary"], {"migrated": 3})
        self.assertEqual(entry["duration_seconds"], 1.23)
        self.assertEqual(entry["backups"], ["/a", "/b"])
        self.assertIn("applied_at", entry)

    def test_mark_applied_is_idempotent(self):
        ledger = Ledger.load(self.tmp)
        ledger.mark_applied("0001_abc", {}, 0.0)
        ledger.mark_applied("0001_abc", {}, 0.0)
        self.assertEqual(len(ledger.applied), 1)

    def test_atomic_write_cleans_up_tmp_on_replace_failure(self):
        ledger = Ledger.load(self.tmp)
        parent = os.path.dirname(os.path.join(self.tmp, LEDGER_FILENAME))
        before = set(os.listdir(parent))
        with patch(
            "apps.utils.ops_migrations.ledger.os.replace",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                ledger.mark_applied("0001_abc", {}, 0.0)
        after = set(os.listdir(parent))
        leftover = after - before
        self.assertFalse(
            any(f.startswith(".greffer-mig-") for f in leftover),
            f"atomic write left tmp files: {leftover}",
        )


class LegacySentinelShimTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def test_sentinel_seeds_ledger_and_is_unlinked(self):
        sentinel = os.path.join(self.tmp, LEGACY_SENTINEL_FILENAME)
        with open(sentinel, "w"):
            pass
        ledger = Ledger.load(self.tmp)
        self.assertTrue(ledger.is_applied(LEGACY_SENTINEL_MIGRATES))
        self.assertFalse(os.path.exists(sentinel))

        # Ledger file got persisted so a second load without the sentinel is still applied.
        fresh = Ledger.load(self.tmp)
        self.assertTrue(fresh.is_applied(LEGACY_SENTINEL_MIGRATES))

    def test_sentinel_shim_idempotent_when_ledger_already_has_id(self):
        # Seed ledger first.
        ledger = Ledger.load(self.tmp)
        ledger.mark_applied(LEGACY_SENTINEL_MIGRATES, {}, 0.0)
        self.assertEqual(len(ledger.applied), 1)
        # Drop the sentinel afterwards; load should unlink but not duplicate.
        sentinel = os.path.join(self.tmp, LEGACY_SENTINEL_FILENAME)
        with open(sentinel, "w"):
            pass
        fresh = Ledger.load(self.tmp)
        self.assertEqual(
            [e["id"] for e in fresh.applied].count(LEGACY_SENTINEL_MIGRATES),
            1,
        )
        self.assertFalse(os.path.exists(sentinel))


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
