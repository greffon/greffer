"""Tests for the 0003 migration (NamespaceUndeclaredVolumeMounts).

0003 exists because 0001 is ledger-recorded and never re-runs: instances
whose composes were rendered after 0001 ran still hold UNDECLARED mount
sources in raw-name docker volumes, and the Feature-3 render change would
mount them empty. The migration copies raw -> <id>_raw, non-destructively.
"""
import os
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

import yaml


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _write_compose(instance_dir, volumes_keys):
    os.makedirs(instance_dir, exist_ok=True)
    compose = {
        "services": {"app": {"image": "nginx"}},
        "volumes": {k: {"name": k} for k in volumes_keys},
    }
    with open(os.path.join(instance_dir, "docker-compose.yml"), "w") as f:
        yaml.safe_dump(compose, f)


def _mig():
    from apps.utils.ops_migrations.migrations._0003_namespace_undeclared_volume_mounts import (
        NamespaceUndeclaredVolumeMounts,
    )
    return NamespaceUndeclaredVolumeMounts()


class NamespaceUndeclaredVolumeMountsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def test_no_data_root_is_no_op(self):
        self.assertEqual(
            _mig().run("/this/does/not/exist"),
            {"migrated": 0, "skipped": 0, "errors": 0},
        )

    @patch(
        "apps.utils.ops_migrations.migrations._0003_namespace_undeclared_volume_mounts.subprocess.run"
    )
    def test_copies_raw_undeclared_volume(self, mock_run):
        # The codex-finding scenario: instance rendered after 0001 ran, with
        # an undeclared source 'dd' — raw volume exists, namespaced doesn't.
        def inspect(args, **_):
            from unittest.mock import MagicMock

            m = MagicMock()
            m.returncode = 0 if args[-1] == "dd" else 1
            return m

        mock_run.side_effect = inspect
        _write_compose(os.path.join(self.tmp, "i1"), ["i1_db", "dd"])
        summary = _mig().run(self.tmp)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["skipped"], 1)  # i1_db already prefixed
        created = [a.args[0][-1] for a in mock_run.call_args_list
                   if a.args[0][1] == "volume"]
        self.assertIn("i1_dd", created)

    @patch(
        "apps.utils.ops_migrations.migrations._0003_namespace_undeclared_volume_mounts.subprocess.run"
    )
    def test_skips_when_target_already_exists(self, mock_run):
        # 0001 already made the copy (or a partial retry): never touch
        # either side.
        mock_run.return_value.returncode = 0  # both volumes "exist"
        _write_compose(os.path.join(self.tmp, "i1"), ["dd"])
        summary = _mig().run(self.tmp)
        self.assertEqual(summary, {"migrated": 0, "skipped": 1, "errors": 0})
        self.assertEqual(mock_run.call_count, 2)  # inspect dd, inspect i1_dd

    @patch(
        "apps.utils.ops_migrations.migrations._0003_namespace_undeclared_volume_mounts.subprocess.run"
    )
    def test_skips_when_source_never_created(self, mock_run):
        mock_run.return_value.returncode = 1
        _write_compose(os.path.join(self.tmp, "i1"), ["never-booted"])
        self.assertEqual(
            _mig().run(self.tmp),
            {"migrated": 0, "skipped": 1, "errors": 0},
        )

    @patch(
        "apps.utils.ops_migrations.migrations._0003_namespace_undeclared_volume_mounts.subprocess.run"
    )
    def test_copy_failure_counts_error_not_raise(self, mock_run):
        from unittest.mock import MagicMock

        def inspect(args, **_):
            m = MagicMock()
            m.returncode = 0 if args[-1] == "dd" else 1
            return m

        def create_fails(args, **_):
            if args[1] == "volume" and args[2] == "create":
                raise subprocess.CalledProcessError(1, args, stderr=b"boom")
            return inspect(args)

        mock_run.side_effect = create_fails
        _write_compose(os.path.join(self.tmp, "i1"), ["dd"])
        summary = _mig().run(self.tmp)
        self.assertEqual(summary["errors"], 1)

    def test_instance_without_compose_skipped(self):
        os.makedirs(os.path.join(self.tmp, "i2"))
        self.assertEqual(
            _mig().run(self.tmp), {"migrated": 0, "skipped": 0, "errors": 0}
        )

    def test_registry_contains_0003(self):
        from apps.utils.ops_migrations.registry import all_migrations

        ids = [m.id for m in all_migrations()]
        self.assertIn("0003_namespace_undeclared_volume_mounts", ids)
        self.assertLess(
            ids.index("0002_purge_staged_key_strays"),
            ids.index("0003_namespace_undeclared_volume_mounts"),
        )

