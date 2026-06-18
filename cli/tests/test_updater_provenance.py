"""Tests for the v2 updater image-provenance primitives (provenance).

The cosign/docker subprocess layer (``compose._run``) is monkeypatched, so no
real registry or cosign binary. (The ``:latest`` redesign dropped the
``min_supported`` floor + signed-manifest + ratchet machinery, HLD section 11, so
there is no longer a floor module to test here.)
"""

from __future__ import annotations

import json

from greffer_cli import compose
from greffer_cli.updater import provenance


def _manifest(digest: str) -> compose.CommandResult:
    """A `docker buildx imagetools inspect --format '{{json .Manifest}}'` body."""
    return compose.CommandResult(
        0, json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json",
                       "digest": digest, "size": 1609}), "")


def _ok(out: str = "") -> compose.CommandResult:
    return compose.CommandResult(0, out, "")


def _fail() -> compose.CommandResult:
    return compose.CommandResult(1, "", "boom")


# --- digest resolution -----------------------------------------------

def test_resolve_digest_reads_index_digest_from_manifest_json(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _manifest("sha256:" + "a" * 64))
    assert provenance.resolve_digest("greffon/greffer:0.3.5") == "sha256:" + "a" * 64
    # and it asks for the JSON manifest, not the fragile {{.Manifest.Digest}}
    seen = {}
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: (seen.update(args=a), _manifest("sha256:" + "b" * 64))[1])
    provenance.resolve_digest("greffon/greffer:0.3.5")
    assert "{{json .Manifest}}" in seen["args"]


def test_resolve_digest_rejects_malformed_digest(monkeypatch):
    for bad in ("", "sha256:zz", "deadbeef", "sha256:" + "a" * 63, "sha256:" + "g" * 64):
        monkeypatch.setattr(compose, "_run", lambda a, _b=bad, **k: _manifest(_b))
        assert provenance.resolve_digest("greffon/greffer:0.3.5") is None


def test_resolve_digest_handles_verbose_fallback_and_errors(monkeypatch):
    # REGRESSION: on a newer buildx, `--format` is ignored and the verbose
    # listing is printed; the old {{.Manifest.Digest}} code read that as a digest
    # and returned None. Now non-JSON fails closed cleanly.
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: _ok("Name: docker.io/greffon/greffer:0.3.6\n"
                                           "Digest: sha256:" + "a" * 64 + "\n"))
    assert provenance.resolve_digest("greffon/greffer:0.3.5") is None
    # JSON without a digest field
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok('{"mediaType": "x"}'))
    assert provenance.resolve_digest("greffon/greffer:0.3.5") is None
    # subprocess failure
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert provenance.resolve_digest("greffon/greffer:0.3.5") is None


# --- cosign verify + repo binding ------------------------------------

def test_cosign_verify_binds_repo_annotation(monkeypatch):
    seen = {}
    monkeypatch.setattr(compose, "_run", lambda a, **k: (seen.update(args=a), _ok())[1])
    digest = "sha256:" + "b" * 64
    assert provenance.cosign_verify("greffon/greffer-nginx", digest, pubkey="/k") is True
    a = seen["args"]
    assert a[:2] == ["cosign", "verify"]
    assert a[a.index("--key") + 1] == "/k"
    # the repo binding is what stops a same-key cross-image signature swap;
    # short -a (== --annotations) matches what the publish CI signs with
    assert a[a.index("-a") + 1] == "repo=greffon/greffer-nginx"
    # offline managed-key model (no Rekor): verify must not require a tlog entry,
    # mirroring the publish CI's --tlog-upload=false
    assert "--insecure-ignore-tlog=true" in a
    assert a[-1] == f"greffon/greffer-nginx@{digest}"


def test_cosign_verify_fail_closed(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert provenance.cosign_verify("greffon/greffer", "sha256:" + "c" * 64) is False


# --- version label ---------------------------------------------------

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
