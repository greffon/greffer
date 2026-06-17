"""Tests for the v2 socket-only recreate primitives (updater.recreate).

docker is invoked through ``greffer_cli.compose._run``, monkeypatched here so
there is no real docker / cosign / registry. Focus: stack discovery by compose
service label (HLD section 4), verify-then-pull (pull-by-digest then retag
:latest, sections 3 + 13), and the :previous / dangling-prune primitives,
including the fail-closed paths.
"""

from __future__ import annotations

import pytest

from greffer_cli import compose
from greffer_cli.updater import provenance, recreate

_D = "sha256:" + "a" * 64


def _ok(out: str = "") -> compose.CommandResult:
    return compose.CommandResult(0, out, "")


def _fail(err: str = "boom") -> compose.CommandResult:
    return compose.CommandResult(1, "", err)


# --- discovery (HLD section 4) --------------------------------------

def _make_discover_fake(
    *, greffer_ids: str = "gid", project: str = "greffer",
    listing: str = "gid\tgreffer\nnid\tnginx\nsid\ttunnel-sidecar\n",
    ps1_ok: bool = True, inspect_ok: bool = True, ps2_ok: bool = True,
):
    """A compose._run double that answers the three discovery docker calls:
    find-greffer-by-service-label, project-of, list-project-containers."""
    def fake(args, *, timeout=None):
        sub = args[1:]
        if sub[0] == "ps" and "label=com.docker.compose.service=greffer" in sub:
            return _ok(greffer_ids + "\n") if ps1_ok else _fail()
        if sub[0] == "inspect" and any("com.docker.compose.project" in a for a in sub):
            return _ok(project + "\n") if inspect_ok else _fail()
        if sub[0] == "ps" and any(
            a.startswith("label=com.docker.compose.project=") for a in sub
        ):
            return _ok(listing) if ps2_ok else _fail()
        return _ok()
    return fake


def test_discover_stack_orders_and_maps_repos(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake())
    stack = recreate.discover_stack()
    # ordered per RECREATE_ORDER (nginx, greffer, tunnel-sidecar), NOT listing order
    assert [c.service for c in stack] == ["nginx", "greffer", "tunnel-sidecar"]
    # repo mapped from the SERVICE label, never the image name
    assert [c.repo for c in stack] == [
        "greffon/greffer-nginx", "greffon/greffer", "greffon/tunnel-sidecar"]
    assert [c.container_id for c in stack] == ["nid", "gid", "sid"]


def test_discover_no_greffer_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(greffer_ids=""))
    assert recreate.discover_stack() == []


def test_discover_ps_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(ps1_ok=False))
    assert recreate.discover_stack() == []


def test_discover_no_project_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(inspect_ok=False))
    assert recreate.discover_stack() == []


def test_discover_listing_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(ps2_ok=False))
    assert recreate.discover_stack() == []


def test_discover_ignores_unknown_services(monkeypatch):
    listing = "gid\tgreffer\nxid\tsome-other\nnid\tnginx\n"
    monkeypatch.setattr(compose, "_run", _make_discover_fake(listing=listing))
    stack = recreate.discover_stack()
    # 'some-other' is not in SERVICE_REPO -> dropped; sidecar absent -> not present
    assert [c.service for c in stack] == ["nginx", "greffer"]


# --- verify-then-pull (HLD sections 3 + 13) -------------------------

def _patch_provenance(monkeypatch, *, digest=_D, cosign_ok=True):
    monkeypatch.setattr(provenance, "resolve_digest", lambda ref: digest)
    monkeypatch.setattr(provenance, "cosign_verify", lambda repo, d, **k: cosign_ok)


def test_verify_then_pull_pulls_by_digest_then_retags(monkeypatch):
    _patch_provenance(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    out = recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")
    assert out == _D
    verbs = [c[0] for c in calls]
    # only the VERIFIED digest is pulled, and the retag happens AFTER the pull
    assert ["pull", f"greffon/greffer@{_D}"] in calls
    assert ["tag", f"greffon/greffer@{_D}", "greffon/greffer:latest"] in calls
    assert verbs.index("pull") < verbs.index("tag")


def test_verify_then_pull_unresolvable_digest_refuses(monkeypatch):
    monkeypatch.setattr(provenance, "resolve_digest", lambda ref: None)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no docker when digest unresolved"))
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


def test_verify_then_pull_cosign_failure_refuses_before_pull(monkeypatch):
    _patch_provenance(monkeypatch, cosign_ok=False)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no pull when cosign fails"))
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


def test_verify_then_pull_pull_failure_refuses_before_retag(monkeypatch):
    _patch_provenance(monkeypatch)

    def fake(args, *, timeout=None):
        if args[1] == "pull":
            return _fail()
        if args[1] == "tag":
            pytest.fail("retag reached after a failed pull")
        return _ok()
    monkeypatch.setattr(compose, "_run", fake)
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


def test_verify_then_pull_retag_failure_refuses(monkeypatch):
    _patch_provenance(monkeypatch)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: _fail() if a[1] == "tag" else _ok())
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


# --- :previous tag, image-id capture, dangling prune ----------------

def test_current_image_id_reads_latest(monkeypatch):
    monkeypatch.setattr(
        compose, "_run",
        lambda a, **k: _ok("sha256:deadbeef\n") if a[1:3] == ["image", "inspect"] else _ok())
    assert recreate.current_image_id("greffon/greffer") == "sha256:deadbeef"


def test_current_image_id_absent_is_none(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert recreate.current_image_id("greffon/greffer") is None


def test_tag_previous_tags_outgoing_image(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    assert recreate.tag_previous("greffon/greffer", "sha256:old") is True
    assert ["tag", "sha256:old", "greffon/greffer:previous"] in calls


def test_tag_previous_empty_id_is_noop(monkeypatch):
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no docker for an empty image id"))
    assert recreate.tag_previous("greffon/greffer", "") is False


def test_tag_previous_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert recreate.tag_previous("greffon/greffer", "sha256:old") is False


def test_dangling_prune_is_dangling_only(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    recreate.dangling_prune()
    assert ["image", "prune", "-f"] in calls
    # never -a (that would reap the tagged :previous / unused images) and never rmi
    assert not any("-a" in c for c in calls)
    assert not any(c and c[0] == "rmi" for c in calls)
