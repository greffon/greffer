"""Unit tests for the persisted greffer token helper."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.token import load_or_create_token, load_persisted_token


def test_load_persisted_returns_token_when_present(tmp_path: Path) -> None:
    path = tmp_path / ".greffer-token"
    path.write_text("on-disk-tok", encoding="utf-8")
    assert load_persisted_token(path) == "on-disk-tok"


def test_load_persisted_returns_none_when_absent(tmp_path: Path) -> None:
    # Never mints (unlike load_or_create_token): a missing file is None so the
    # caller keeps its stable token instead of churning a fresh ephemeral one.
    assert load_persisted_token(tmp_path / ".greffer-token") is None


def test_load_persisted_returns_none_for_blank_file(tmp_path: Path) -> None:
    path = tmp_path / ".greffer-token"
    path.write_text("  \n", encoding="utf-8")
    assert load_persisted_token(path) is None


def test_mints_and_persists_when_absent(tmp_path: Path) -> None:
    path = tmp_path / ".greffer-token"
    token = load_or_create_token(path)
    assert isinstance(token, str) and len(token) >= 32
    # Written to disk, and what's on disk matches what was returned.
    assert path.read_text(encoding="utf-8").strip() == token


def test_reuses_existing_token(tmp_path: Path) -> None:
    path = tmp_path / ".greffer-token"
    path.write_text("preexisting-token", encoding="utf-8")
    assert load_or_create_token(path) == "preexisting-token"


def test_stable_across_calls(tmp_path: Path) -> None:
    """The whole point: repeated calls (process restarts) return the same
    token once one has been persisted."""
    path = tmp_path / ".greffer-token"
    first = load_or_create_token(path)
    second = load_or_create_token(path)
    assert first == second


def test_ignores_blank_file_and_mints(tmp_path: Path) -> None:
    """A whitespace-only file (e.g. a truncated write) is treated as absent —
    we don't hand the manager an empty token."""
    path = tmp_path / ".greffer-token"
    path.write_text("   \n", encoding="utf-8")
    token = load_or_create_token(path)
    assert token.strip() == token and len(token) >= 32
    assert path.read_text(encoding="utf-8").strip() == token


def test_token_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / ".greffer-token"
    load_or_create_token(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / ".greffer-token"
    token = load_or_create_token(path)
    assert path.read_text(encoding="utf-8").strip() == token


def test_falls_back_to_ephemeral_when_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If persistence fails (read-only volume), the greffer still boots with an
    in-memory token rather than crashing — degraded, not broken."""
    path = tmp_path / ".greffer-token"

    def _boom(*_a, **_k):
        raise OSError("read-only file system")

    # Make the atomic write fail at the mkstemp step (can't create the temp).
    monkeypatch.setattr("app.token.tempfile.mkstemp", _boom)
    token = load_or_create_token(path)
    assert isinstance(token, str) and len(token) >= 32
    assert not path.exists()  # nothing persisted


def test_no_temp_file_left_behind_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the publish (os.replace) fails after the temp file is written, the
    temp file must be cleaned up, not left lying around with the secret."""
    path = tmp_path / ".greffer-token"

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("app.token.os.replace", _boom)
    token = load_or_create_token(path)
    # Fallback token returned, nothing published, and no temp leftover.
    assert isinstance(token, str) and len(token) >= 32
    assert not path.exists()
    leftovers = list(tmp_path.glob(".greffer-token.*.tmp"))
    assert leftovers == []


def test_resolve_token_prefers_settings_override(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from app.token import resolve_token
    s = SimpleNamespace(greffer_token="operator-tok", greffon_path=tmp_path)
    assert resolve_token(s) == "operator-tok"
    # Disk token file is never created when the override wins.
    assert not (tmp_path / ".greffer-token").exists()


def test_resolve_token_falls_back_to_disk(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from app.token import resolve_token
    s = SimpleNamespace(greffer_token=None, greffon_path=tmp_path)
    tok = resolve_token(s)
    assert tok
    # Minted and persisted under greffon_path.
    assert (tmp_path / ".greffer-token").read_text().strip() == tok
