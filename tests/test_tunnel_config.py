"""Tests for the v3 manager-pushed client.toml writer.

The writer is a single helper module with two entry points:
``write_client_toml`` (always writes; raises on OS error) and
``maybe_write_client_toml`` (no-op when content is None or path is
empty). Both are exercised here, plus the atomicity property the
controller handler depends on.

See tunnel-support epic v3 §"Changes from v2" §4 and the AC for
atomic writes ("greffer-side controller handler uses os.replace()
... concurrent start + stop must not produce torn writes").
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.tunnel_config import (
    TunnelConfigWriteError,
    maybe_write_client_toml,
    write_client_toml,
)


def test_write_client_toml_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "client.toml"
    write_client_toml('[client]\nremote_addr = "x"\n', target)
    assert target.read_text() == '[client]\nremote_addr = "x"\n'


def test_write_client_toml_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "client.toml"
    target.write_text("OLD")
    write_client_toml("NEW", target)
    assert target.read_text() == "NEW"


def test_write_client_toml_raises_on_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "does-not-exist" / "client.toml"
    with pytest.raises(TunnelConfigWriteError):
        write_client_toml("anything", target)


def test_write_client_toml_leaves_no_temp_files_on_success(
    tmp_path: Path,
) -> None:
    """Successful write must clean up the staging temp file via the
    rename. Lingering ``.client.toml.*.tmp`` files would confuse
    rathole-client's file-watcher (it ignores hidden files but a
    growing pile of them is still messy)."""
    target = tmp_path / "client.toml"
    write_client_toml("hello", target)
    siblings = [p for p in tmp_path.iterdir() if p.name != "client.toml"]
    assert siblings == []


def test_write_client_toml_leaves_no_temp_files_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure path: simulate os.replace raising and verify the staging
    tmp file is cleaned up. Without this, repeated failures would
    accumulate temp files in the config directory."""
    target = tmp_path / "client.toml"
    real_replace = os.replace

    def boom(_src: str, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(TunnelConfigWriteError):
        write_client_toml("payload", target)
    monkeypatch.setattr(os, "replace", real_replace)

    leftovers = list(tmp_path.iterdir())
    assert leftovers == [], f"leftover files: {leftovers}"


# ---------------------------------------------------------------------------
# Atomicity property — concurrent writes don't produce torn output
# ---------------------------------------------------------------------------


def test_concurrent_writes_never_produce_torn_output(
    tmp_path: Path,
) -> None:
    """AC from the v3 epic: 'concurrent start + stop on the same greffer
    must not produce torn writes; final on-disk file must be byte-
    identical to one of the two inputs.'

    Race two writers against the same target. Repeat enough times that
    a non-atomic implementation (open(...).write() instead of
    tempfile + os.replace) would have many chances to interleave.
    Assert the final file is always exactly one of the two payloads.
    """
    target = tmp_path / "client.toml"
    payload_a = "A" * 4096
    payload_b = "B" * 4096

    valid_outcomes = {payload_a, payload_b}

    for _ in range(50):
        # Pre-populate so neither writer is creating-from-scratch — the
        # interesting race is mid-flight, not the empty-file edge case.
        target.write_text("seed")
        threads = [
            threading.Thread(
                target=write_client_toml, args=(payload_a, target)
            ),
            threading.Thread(
                target=write_client_toml, args=(payload_b, target)
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        actual = target.read_text()
        assert actual in valid_outcomes, (
            f"torn write detected: file content was neither A's nor B's "
            f"full payload (length={len(actual)}, "
            f"first chars={actual[:32]!r})"
        )


# ---------------------------------------------------------------------------
# maybe_write_client_toml — short-circuits on None / empty path
# ---------------------------------------------------------------------------


def test_maybe_write_skips_when_content_is_none(tmp_path: Path) -> None:
    target = tmp_path / "client.toml"
    wrote = maybe_write_client_toml(None, target)
    assert wrote is False
    assert not target.exists()


def test_maybe_write_skips_when_path_is_empty(tmp_path: Path) -> None:
    wrote = maybe_write_client_toml("payload", "")
    assert wrote is False


def test_maybe_write_writes_when_both_provided(tmp_path: Path) -> None:
    target = tmp_path / "client.toml"
    wrote = maybe_write_client_toml("payload", target)
    assert wrote is True
    assert target.read_text() == "payload"


def test_maybe_write_propagates_oserror(tmp_path: Path) -> None:
    """maybe_write doesn't swallow OSError — the controller handler
    relies on the exception propagating so it can map to
    config_write_status='failed'."""
    target = tmp_path / "missing-dir" / "client.toml"
    with pytest.raises(TunnelConfigWriteError):
        maybe_write_client_toml("payload", target)
