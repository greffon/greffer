"""Tests for ``app/cli.py`` — argparse wiring + exit codes.

Locks the contract the Django management command exposed before
cutover: same 5 flags (``--dry-run``, ``--only``, ``--fail-fast``,
``--restore``, ``--data-root``), same 3 exit codes (0 / 1 / 2).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.cli import main
from app.settings import Settings


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_exit_code_0_when_no_pending_migrations(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = []
        rc = main(["apply_ops_migrations"])
    assert rc == 0
    assert "no pending migrations" in capsys.readouterr().out


def test_exit_code_0_on_all_success(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    ok_result = MagicMock(ok=True, id="0001_foo", duration_seconds=0.1, summary={"migrated": 1})
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = [ok_result]
        rc = main(["apply_ops_migrations"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "0001_foo" in out


def test_exit_code_1_on_failure(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    fail_result = MagicMock(ok=False, id="0001_bad", duration_seconds=0.2, error="boom")
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = [fail_result]
        rc = main(["apply_ops_migrations"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err
    assert "0001_bad" in err


def test_exit_code_2_on_unknown_only_id(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    with patch("app.cli.all_migrations") as mock_all:
        mock_all.return_value = [MagicMock(id="0001_known")]
        rc = main(["apply_ops_migrations", "--only", "0002_missing"])
    assert rc == 2
    assert "no migration with that id registered" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Flag plumbing
# ---------------------------------------------------------------------------


def test_dry_run_flag_passed_through(settings: Settings) -> None:
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = []
        main(["apply_ops_migrations", "--dry-run"])
    assert mock_runner.apply_pending.call_args.kwargs["dry_run"] is True


def test_fail_fast_flag_passed_through(settings: Settings) -> None:
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = []
        main(["apply_ops_migrations", "--fail-fast"])
    assert mock_runner.apply_pending.call_args.kwargs["fail_fast"] is True


def test_only_flag_passed_through(settings: Settings) -> None:
    with patch("app.cli.runner") as mock_runner, patch(
        "app.cli.all_migrations"
    ) as mock_all:
        mock_all.return_value = [MagicMock(id="0001_x")]
        mock_runner.apply_pending.return_value = []
        main(["apply_ops_migrations", "--only", "0001_x"])
    assert mock_runner.apply_pending.call_args.kwargs["only"] == "0001_x"


def test_data_root_override(settings: Settings) -> None:
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = []
        main(["apply_ops_migrations", "--data-root", "/tmp/alt"])
    assert mock_runner.apply_pending.call_args.kwargs["data_root"] == "/tmp/alt"


def test_default_data_root_from_settings(settings: Settings) -> None:
    """No ``--data-root`` flag → use ``settings.greffon_path``."""
    with patch("app.cli.runner") as mock_runner:
        mock_runner.apply_pending.return_value = []
        main(["apply_ops_migrations"])
    assert mock_runner.apply_pending.call_args.kwargs["data_root"] == str(
        settings.greffon_path
    )


# ---------------------------------------------------------------------------
# --restore
# ---------------------------------------------------------------------------


def test_restore_prints_paths_and_returns_0(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    with patch("app.cli.operations") as mock_ops:
        mock_ops.restore.return_value = ["/backup/a", "/backup/b"]
        rc = main(["apply_ops_migrations", "--restore", "0001_x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/backup/a" in out
    assert "/backup/b" in out


def test_restore_no_backups_returns_0_with_warning(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    with patch("app.cli.operations") as mock_ops:
        mock_ops.restore.return_value = []
        rc = main(["apply_ops_migrations", "--restore", "0001_x"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no backups recorded" in err
