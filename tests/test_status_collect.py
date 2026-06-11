"""Tests for the shared status collector (greffer-observability epic)."""
from __future__ import annotations

from unittest.mock import patch

# Force import of the lazy compose submodule so its attribute exists for patching.
import apps.utils.docker.compose  # noqa: F401

from app.settings import Settings
from app.workers.status_collect import collect_status_map


def test_collect_status_map_skips_dotfiles(settings: Settings, tmp_path) -> None:
    (tmp_path / "inst-a").mkdir()
    (tmp_path / "inst-b").mkdir()
    (tmp_path / ".greffer-token").write_text("secret")
    (tmp_path / ".greffer-migrations.lock").write_text("")
    settings.greffon_path = tmp_path  # type: ignore[misc]

    with patch("apps.utils.docker.compose") as mock_compose:
        mock_compose.get_status.side_effect = lambda gid: {
            "inst-a": {"status": "running"},
            "inst-b": {"status": "stopped"},
        }[gid]
        result = collect_status_map(settings)

    assert result == {"inst-a": "running", "inst-b": "stopped"}
    # Dotfiles are never queried.
    queried = {c.args[0] for c in mock_compose.get_status.call_args_list}
    assert queried == {"inst-a", "inst-b"}
