"""Tests for volume_migration.run().

The migration is hard to exercise against real docker without a docker-in-
docker harness, so these tests mock out subprocess.run at the boundary and
assert the decision logic (which volumes get migrated, which get skipped,
how the sentinel is managed, error handling).
"""
import json
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


class VolumeMigrationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, self.tmp)

    def test_no_data_root_is_no_op(self):
        from apps.utils.docker.volume_migration import run
        summary = run(data_root="/this/does/not/exist")
        self.assertEqual(summary, {"migrated": 0, "skipped": 0, "errors": 0})

    def test_sentinel_short_circuits_second_run(self):
        from apps.utils.docker.volume_migration import run, SENTINEL_NAME
        sentinel = os.path.join(self.tmp, SENTINEL_NAME)
        with open(sentinel, "w"):
            pass
        # Even a dir full of stuff shouldn't be touched once the sentinel is there.
        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "db_data"}},
        )
        summary = run(data_root=self.tmp)
        self.assertEqual(summary, {"migrated": 0, "skipped": 0, "errors": 0})

    @patch("apps.utils.docker.volume_migration.subprocess.run")
    def test_migrates_unprefixed_volume(self, mock_run):
        """A compose declaring `name: db_data` gets its data copied from the
        shared `db_data` volume into `<uuid>_db_data`."""
        from apps.utils.docker.volume_migration import run

        instance_id = "abc-1234"
        _write_compose(
            os.path.join(self.tmp, instance_id),
            {"db_data": {"name": "db_data"}},
        )

        # docker volume inspect: old exists (rc=0), new doesn't (rc=1)
        # docker volume create / docker run: succeed (rc=0)
        def fake(cmd, **kw):
            r = MagicMock()
            if cmd[:3] == ["docker", "volume", "inspect"]:
                r.returncode = 0 if cmd[-1] == "db_data" else 1
            else:
                r.returncode = 0
            r.stderr = b""
            return r
        mock_run.side_effect = fake

        summary = run(data_root=self.tmp)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["errors"], 0)

        # Sentinel was written.
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".volumes-migrated")))

        # docker volume create was called with the namespaced name.
        create_calls = [c for c in mock_run.call_args_list
                        if c.args and c.args[0][:3] == ["docker", "volume", "create"]]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0].args[0][-1], f"{instance_id}_db_data")

    @patch("apps.utils.docker.volume_migration.subprocess.run")
    def test_skips_when_old_volume_missing(self, mock_run):
        from apps.utils.docker.volume_migration import run

        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "db_data"}},
        )
        # Every inspect returns rc=1 → nothing exists, nothing to migrate.
        r = MagicMock(); r.returncode = 1; r.stderr = b""
        mock_run.return_value = r

        summary = run(data_root=self.tmp)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(summary["skipped"], 1)

    @patch("apps.utils.docker.volume_migration.subprocess.run")
    def test_skips_when_already_namespaced(self, mock_run):
        from apps.utils.docker.volume_migration import run

        _write_compose(
            os.path.join(self.tmp, "abc"),
            {"db_data": {"name": "abc_db_data"}},
        )
        r = MagicMock(); r.returncode = 0; r.stderr = b""
        mock_run.return_value = r

        summary = run(data_root=self.tmp)
        self.assertEqual(summary["migrated"], 0)
        # Was already in the new scheme.
        self.assertEqual(summary["skipped"], 1)
        # No volume create / docker run was issued.
        for c in mock_run.call_args_list:
            self.assertNotEqual(c.args[0][:3], ["docker", "volume", "create"])

    @patch("apps.utils.docker.volume_migration.subprocess.run")
    def test_skips_when_target_already_exists(self, mock_run):
        """If both <uuid>_db_data AND db_data already exist (partial retry),
        don't touch either."""
        from apps.utils.docker.volume_migration import run

        instance_id = "abc"
        _write_compose(
            os.path.join(self.tmp, instance_id),
            {"db_data": {"name": "db_data"}},
        )

        def fake(cmd, **kw):
            r = MagicMock(); r.returncode = 0; r.stderr = b""
            if cmd[:3] == ["docker", "volume", "inspect"]:
                # Both old and new exist — skip.
                r.returncode = 0
            return r
        mock_run.side_effect = fake

        summary = run(data_root=self.tmp)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(summary["skipped"], 1)

    @patch("apps.utils.docker.volume_migration.subprocess.run")
    def test_errors_prevent_sentinel(self, mock_run):
        """If docker volume create fails, sentinel isn't written so the next
        boot retries."""
        from apps.utils.docker.volume_migration import run

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

        summary = run(data_root=self.tmp)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["migrated"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".volumes-migrated")))

    def test_malformed_compose_is_skipped(self):
        from apps.utils.docker.volume_migration import run

        os.makedirs(os.path.join(self.tmp, "abc"))
        with open(os.path.join(self.tmp, "abc", "docker-compose.yml"), "w") as f:
            f.write("::: not yaml :::")

        # Should not raise; returns clean summary.
        summary = run(data_root=self.tmp)
        self.assertEqual(summary["errors"], 0)

    @patch("apps.utils.docker.volume_migration.subprocess.run")
    def test_does_not_double_prefix_nginx_volume(self, mock_run):
        """Regression: `greffon_nginx` volume was already namespaced
        pre-migration (repository.py set `value: <uuid>_nginx_volume`). A naive
        migration that builds `expected = <uuid>_<declared>` where declared is
        already `<uuid>_nginx_volume` would create `<uuid>_<uuid>_nginx_volume`."""
        from apps.utils.docker.volume_migration import run

        instance_id = "abc-1234"
        # This mirrors what repository.py writes for the nginx volume —
        # declared key AND effective name already include the instance_id.
        _write_compose(
            os.path.join(self.tmp, instance_id),
            {f"{instance_id}_nginx_volume": {"name": f"{instance_id}_nginx_volume"}},
        )
        r = MagicMock(); r.returncode = 0; r.stderr = b""
        mock_run.return_value = r

        summary = run(data_root=self.tmp)
        self.assertEqual(summary["migrated"], 0)
        # Should have skipped — no `docker volume create` called.
        for c in mock_run.call_args_list:
            self.assertNotEqual(c.args[0][:3], ["docker", "volume", "create"])


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
