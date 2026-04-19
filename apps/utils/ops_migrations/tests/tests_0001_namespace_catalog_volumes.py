"""Tests for the 0001 migration (NamespaceCatalogVolumes).

Moved from `apps/utils/docker/tests_volume_migration.py` — same 9 cases with
mechanical import/call-site updates. Sentinel-short-circuit logic moved from
the migration body into `ledger.py` and is covered by `tests_ledger.py`.
"""
import os
import subprocess
import tempfile
from unittest.mock import patch, MagicMock

import yaml
from django.test import TestCase


def _write_compose(instance_dir, volumes_spec):
    os.makedirs(instance_dir, exist_ok=True)
    compose = {
        "services": {"app": {"image": "nginx"}},
        "volumes": volumes_spec,
    }
    with open(os.path.join(instance_dir, "docker-compose.yml"), "w") as f:
        yaml.safe_dump(compose, f)


def _mig():
    from apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes import (
        NamespaceCatalogVolumes,
    )
    return NamespaceCatalogVolumes()


class NamespaceCatalogVolumesTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def test_no_data_root_is_no_op(self):
        summary = _mig().run("/this/does/not/exist")
        self.assertEqual(summary, {"migrated": 0, "skipped": 0, "errors": 0})

    @patch(
        "apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes.subprocess.run"
    )
    def test_migrates_unprefixed_volume(self, mock_run):
        """A compose declaring `name: db_data` gets its data copied from the
        shared `db_data` volume into `<uuid>_db_data`."""
        instance_id = "abc-1234"
        _write_compose(
            os.path.join(self.tmp, instance_id),
            {"db_data": {"name": "db_data"}},
        )

        def fake(cmd, **kw):
            r = MagicMock()
            if cmd[:3] == ["docker", "volume", "inspect"]:
                r.returncode = 0 if cmd[-1] == "db_data" else 1
            else:
                r.returncode = 0
            r.stderr = b""
            return r
        mock_run.side_effect = fake

        summary = _mig().run(self.tmp)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["errors"], 0)

        create_calls = [c for c in mock_run.call_args_list
                        if c.args and c.args[0][:3] == ["docker", "volume", "create"]]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0].args[0][-1], f"{instance_id}_db_data")

    @patch(
        "apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes.subprocess.run"
    )
    def test_skips_when_old_volume_missing(self, mock_run):
        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "db_data"}},
        )
        r = MagicMock(); r.returncode = 1; r.stderr = b""
        mock_run.return_value = r

        summary = _mig().run(self.tmp)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(summary["skipped"], 1)

    @patch(
        "apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes.subprocess.run"
    )
    def test_skips_when_already_namespaced(self, mock_run):
        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "abc_db_data"}},
        )
        r = MagicMock(); r.returncode = 0; r.stderr = b""
        mock_run.return_value = r

        summary = _mig().run(self.tmp)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(summary["skipped"], 1)
        for c in mock_run.call_args_list:
            self.assertNotEqual(c.args[0][:3], ["docker", "volume", "create"])

    @patch(
        "apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes.subprocess.run"
    )
    def test_skips_when_target_already_exists(self, mock_run):
        """If both old and new exist (partial retry), don't touch either."""
        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "db_data"}},
        )

        def fake(cmd, **kw):
            r = MagicMock(); r.returncode = 0; r.stderr = b""
            if cmd[:3] == ["docker", "volume", "inspect"]:
                r.returncode = 0  # both exist
            return r
        mock_run.side_effect = fake

        summary = _mig().run(self.tmp)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(summary["skipped"], 1)

    @patch(
        "apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes.subprocess.run"
    )
    def test_errors_counted_without_raising(self, mock_run):
        """If docker volume create fails, errors increments + summary still
        returns. (Runner uses the non-zero errors count to refuse marking
        the migration applied — see tests_runner.py.)"""
        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "db_data"}},
        )

        def fake(cmd, **kw):
            r = MagicMock(); r.returncode = 0; r.stderr = b""
            if cmd[:3] == ["docker", "volume", "inspect"]:
                r.returncode = 0 if cmd[-1] == "db_data" else 1
            elif cmd[:3] == ["docker", "volume", "create"]:
                raise subprocess.CalledProcessError(1, cmd, stderr=b"boom")
            return r
        mock_run.side_effect = fake

        summary = _mig().run(self.tmp)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["migrated"], 0)

    def test_malformed_compose_is_skipped(self):
        os.makedirs(os.path.join(self.tmp, "abc"))
        with open(os.path.join(self.tmp, "abc", "docker-compose.yml"), "w") as f:
            f.write("::: not yaml :::")
        summary = _mig().run(self.tmp)
        self.assertEqual(summary["errors"], 0)

    @patch(
        "apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes.subprocess.run"
    )
    def test_does_not_double_prefix_nginx_volume(self, mock_run):
        """`greffon_nginx` volume was always namespaced pre-migration. A naive
        `expected = <uuid>_<declared>` would build `<uuid>_<uuid>_nginx_volume`."""
        instance_id = "abc-1234"
        _write_compose(
            os.path.join(self.tmp, instance_id),
            {f"{instance_id}_nginx_volume": {"name": f"{instance_id}_nginx_volume"}},
        )
        r = MagicMock(); r.returncode = 0; r.stderr = b""
        mock_run.return_value = r

        summary = _mig().run(self.tmp)
        self.assertEqual(summary["migrated"], 0)
        for c in mock_run.call_args_list:
            self.assertNotEqual(c.args[0][:3], ["docker", "volume", "create"])


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
