"""Tests for the v2 updater verification primitives (provenance + floor).

The cosign/docker subprocess layer (``compose._run``) and the hardened manifest
HTTP opener are monkeypatched, so no real registry, cosign binary, or network.
"""

from __future__ import annotations

import json

import pytest

from greffer_cli import compose, update
from greffer_cli.updater import floor, provenance


def _ok(out: str = "") -> compose.CommandResult:
    return compose.CommandResult(0, out, "")


def _fail() -> compose.CommandResult:
    return compose.CommandResult(1, "", "boom")


# --- provenance: digest resolution -----------------------------------

def test_resolve_digest_ok(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("sha256:" + "a" * 64 + "\n"))
    assert provenance.resolve_digest("greffon/greffer:0.3.5") == "sha256:" + "a" * 64


def test_resolve_digest_rejects_malformed(monkeypatch):
    for bad in ("", "sha256:zz", "deadbeef", "sha256:" + "a" * 63, "sha256:" + "g" * 64):
        monkeypatch.setattr(compose, "_run", lambda a, _b=bad, **k: _ok(_b))
        assert provenance.resolve_digest("greffon/greffer:0.3.5") is None
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert provenance.resolve_digest("greffon/greffer:0.3.5") is None


# --- provenance: cosign verify + repo binding ------------------------

def test_cosign_verify_binds_repo_annotation(monkeypatch):
    seen = {}
    monkeypatch.setattr(compose, "_run", lambda a, **k: (seen.update(args=a), _ok())[1])
    digest = "sha256:" + "b" * 64
    assert provenance.cosign_verify("greffon/greffer-nginx", digest, pubkey="/k") is True
    a = seen["args"]
    assert a[:2] == ["cosign", "verify"]
    assert a[a.index("--key") + 1] == "/k"
    # the repo binding is what stops a same-key cross-image signature swap
    assert a[a.index("--annotation") + 1] == "repo=greffon/greffer-nginx"
    assert a[-1] == f"greffon/greffer-nginx@{digest}"


def test_cosign_verify_fail_closed(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert provenance.cosign_verify("greffon/greffer", "sha256:" + "c" * 64) is False


# --- provenance: version label ---------------------------------------

def test_image_version_reads_label(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("0.3.5\n"))
    assert provenance.image_version("greffon/greffer@sha256:x") == "0.3.5"


def test_image_version_absent_or_error(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("<no value>\n"))
    assert provenance.image_version("x") is None
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("   \n"))
    assert provenance.image_version("x") is None
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert provenance.image_version("x") is None


# --- floor: semver ----------------------------------------------------

def test_parse_and_compare():
    assert floor.parse_version("0.3.5") == (0, 3, 5)
    assert floor.parse_version("latest") is None
    assert floor.version_ge("0.3.5", "0.3.4") is True
    assert floor.version_ge("0.3", "0.3.0") is True       # zero-pad
    assert floor.version_ge("0.3.4", "0.3.5") is False
    assert floor.version_ge("latest", "0.3.0") is False   # unparseable -> fail-closed
    assert floor.higher("0.3.4", "0.3.9") == "0.3.9"
    assert floor.higher(None, "0.3.1") == "0.3.1"
    assert floor.higher("0.3.1", None) == "0.3.1"


# --- floor: signed manifest fetch + verify ---------------------------

class _Resp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self, n: int = -1) -> bytes:
        return self._b if n < 0 else self._b[:n]

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def _patch_opener(monkeypatch, bodies: dict) -> None:
    def opn(url, timeout=None):
        return _Resp(bodies[".sig" if url.endswith(".sig") else ""])
    monkeypatch.setattr(update._MANIFEST_OPENER, "open", opn)


def test_signed_min_supported_happy(monkeypatch):
    manifest = json.dumps({"latest": "0.3.5", "min_supported": "0.3.0"}).encode()
    _patch_opener(monkeypatch, {"": manifest, ".sig": b"SIG"})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok())  # cosign verify-blob ok
    assert floor.signed_min_supported("https://x/m.json", cosign_pub="/k") == "0.3.0"


def test_signed_min_supported_sig_fail_closed(monkeypatch):
    _patch_opener(monkeypatch, {"": b"{}", ".sig": b"SIG"})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())  # cosign rejects
    with pytest.raises(floor.FloorError):
        floor.signed_min_supported("https://x/m.json", cosign_pub="/k")


def test_signed_min_supported_refuses_non_https():
    with pytest.raises(floor.FloorError):
        floor.signed_min_supported("http://x/m.json", cosign_pub="/k")


def test_signed_min_supported_none_when_absent(monkeypatch):
    _patch_opener(monkeypatch, {"": json.dumps({"latest": "0.3.5"}).encode(), ".sig": b"S"})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok())
    assert floor.signed_min_supported("https://x/m.json", cosign_pub="/k") is None


# --- floor: ratchet + effective_floor --------------------------------

def test_ratchet_roundtrip(tmp_path):
    p = tmp_path / "floor"
    assert floor.read_ratchet(p) is None
    floor.write_ratchet(p, "0.3.0")
    assert floor.read_ratchet(p) == "0.3.0"
    floor.write_ratchet(p, "garbage")  # non-version write ignored
    assert floor.read_ratchet(p) == "0.3.0"


def test_effective_floor_takes_max_and_ratchets(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, {"": json.dumps({"min_supported": "0.3.0"}).encode(), ".sig": b"S"})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok())
    p = tmp_path / "floor"
    p.write_text("0.3.5\n")  # ratchet already higher than the manifest
    f = floor.effective_floor("https://x/m.json", baked_baseline="0.2.0",
                              ratchet_path=p, cosign_pub="/k")
    assert f == "0.3.5"                       # max(0.2.0, 0.3.0, 0.3.5)
    assert floor.read_ratchet(p) == "0.3.5"   # ratchet held


def test_effective_floor_replayed_manifest_cannot_lower(monkeypatch, tmp_path):
    # a replayed OLD manifest (min_supported 0.3.0) cannot drop a node whose
    # baked baseline already saw 0.3.5 (the anti-replay guarantee)
    _patch_opener(monkeypatch, {"": json.dumps({"min_supported": "0.3.0"}).encode(), ".sig": b"S"})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok())
    p = tmp_path / "floor"
    f = floor.effective_floor("https://x/m.json", baked_baseline="0.3.5",
                              ratchet_path=p, cosign_pub="/k")
    assert f == "0.3.5"


def test_effective_floor_fail_closed_on_bad_manifest(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, {"": b"x", ".sig": b"S"})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())  # signature fails
    with pytest.raises(floor.FloorError):
        floor.effective_floor("https://x/m.json", baked_baseline="0.3.0",
                              ratchet_path=tmp_path / "f", cosign_pub="/k")
