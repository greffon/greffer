"""Catalog render regression oracle.

The compose body is rendered by ``create_compose`` through a Jinja template.
Any change to that render (the sandbox swap, the undefined policy, a
round-trip fix) touches the exact code path every existing deployment shares,
so the merge gate for those changes is this: render every catalog entry and
prove the bytes did not move.

Snapshots live in ``tests/snapshots/catalog_render/``, next to ``CATALOG_SHA``,
the catalog commit they were made from. Regenerate deliberately with
``CATALOG_RENDER_UPDATE=1 pytest tests/test_catalog_render_regression.py``
against a git checkout of the catalog, review the diff -- a surprise there is
the point of the harness -- and bump the pinned ``ref`` in both workflows to
the new ``CATALOG_SHA``.

Determinism matters more than fidelity here: the fixture pins ``port_host``
and the instance id rather than allocating, because a snapshot that moves on
its own proves nothing. Port allocation is upstream of the render and is not
what these tests guard.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

import pytest
import yaml
from unittest import mock

from apps.utils.docker import compose as compose_mod
from apps.utils.greffon import repository

_HERE = pathlib.Path(__file__).resolve().parent
_SNAP_DIR = _HERE / "snapshots" / "catalog_render"
_UPDATE = os.getenv("CATALOG_RENDER_UPDATE") == "1"


def _catalog_root() -> pathlib.Path | None:
    """Find the catalog, or return None.

    Walk every ancestor rather than guessing a fixed depth: a direct
    ``greffer/`` checkout, a ``greffer-worktrees/<x>`` worktree and a CI
    checkout all sit at different depths, and a hard-coded range silently
    finds nothing -- which would skip this whole oracle while still reporting
    green. ``GREFFON_CATALOG_DIR`` overrides for layouts we cannot guess."""
    env = os.getenv("GREFFON_CATALOG_DIR")
    if env:
        p = pathlib.Path(env)
        return p if p.is_dir() else None
    for ancestor in _HERE.parents:
        cand = ancestor / "greffon-catalog"
        if (cand / "_template").is_dir() or list(cand.glob("*/*/docker-compose.yml"))[:1]:
            return cand
    return None


def _entries():
    root = _catalog_root()
    if root is None:
        return []
    out = []
    for compose_path in sorted(root.glob("*/*/docker-compose.yml")):
        version_dir = compose_path.parent
        name = f"{version_dir.parent.name}/{version_dir.name}"
        if version_dir.parent.name.startswith("_"):
            continue
        out.append((name, compose_path))
    return out


_ENTRIES = _entries()

_SHA_FILE = _SNAP_DIR / "CATALOG_SHA"
_WORKFLOWS = _HERE.parent / ".github" / "workflows"
_MIN_ENTRIES = 25

# Catalog entries known NOT to render, mapped to the reason. Empty: every entry
# in the pinned catalog renders. An entry goes here only on purpose. The oracle
# used to record a failed render AS its snapshot -- the error marker became the
# expected bytes -- so a greffon that cannot deploy at all reported green, and
# the regenerate workflow blessed it without anyone deciding to.
_KNOWN_UNRENDERABLE: dict[str, str] = {}


def _catalog_sha(root):
    """The commit of the catalog checkout under test, or None when it cannot be
    read: not a git checkout, no git, or a plain directory nested inside some
    OTHER repository, whose HEAD would otherwise be reported as the catalog's."""
    if root is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) != 2 or pathlib.Path(out[0]).resolve() != pathlib.Path(root).resolve():
        return None
    return out[1] if re.fullmatch(r"[0-9a-f]{40}", out[1]) else None


def _recorded_sha():
    """The catalog commit the committed snapshots describe."""
    try:
        return _SHA_FILE.read_text().strip() or None
    except OSError:
        return None


_ACTUAL_SHA = _catalog_sha(_catalog_root())
_PINNED_SHA = _recorded_sha()


def _coverage_problem(entries, actual_sha, pinned_sha, check_sha=True):
    """Why the oracle is NOT comparing the whole pinned catalog, or None.

    A function rather than inline asserts so each refusal can be exercised
    directly: the entry floor below was never once seen to fail, and a check
    nothing has seen fail pins nothing."""
    if not entries:
        return ("no greffon-catalog checkout found, so the catalog render "
                "oracle covered NOTHING. Point GREFFON_CATALOG_DIR at a catalog "
                "checkout (CI must fetch one), or set GREFFON_CATALOG_OPTIONAL=1 "
                "to accept the reduced coverage on purpose.")
    if len(entries) < _MIN_ENTRIES:
        return (f"only {len(entries)} catalog entries discovered; the oracle "
                f"is supposed to cover the whole catalog (29 at the pinned "
                f"commit)")
    if not check_sha:
        return None
    if pinned_sha is None:
        return (f"{_SHA_FILE.name} is missing, so nothing records which catalog "
                f"commit the snapshots describe")
    if actual_sha != pinned_sha:
        found = (actual_sha[:12] if actual_sha
                 else "an unreadable commit (not a git checkout?)")
        return (f"the catalog under test is at {found}, but the snapshots were "
                f"made from {pinned_sha[:12]}. Comparing them reports catalog "
                f"drift as render regressions, and regenerating them would break "
                f"CI, which checks out the pinned commit. Point "
                f"GREFFON_CATALOG_DIR at a checkout of {pinned_sha}, e.g. "
                f"`git -C greffon-catalog worktree add --detach <dir> "
                f"{pinned_sha}`.")
    return None


def _render_problem(name, rendered, known_unrenderable):
    """Why this render must be neither compared nor snapshotted, or None."""
    failed = rendered.startswith("<<")
    if failed and name not in known_unrenderable:
        return (f"{name} does not render: {rendered}. A greffon that cannot "
                f"render cannot deploy, so this is a failure, never a snapshot. "
                f"If it is known and accepted, name it in _KNOWN_UNRENDERABLE "
                f"with the reason.")
    if not failed and name in known_unrenderable:
        return (f"{name} renders now; remove it from _KNOWN_UNRENDERABLE so "
                f"the allowlist cannot go stale.")
    return None


def _ports_for(version_dir: pathlib.Path):
    """The manager's start-request port map, keyed ``<service>_<container>``.

    Exposure is manager-authoritative: create_greffon_info reads
    ``greffon['ports'][port_name]`` and defaults to http/tcp when it is absent
    (repository.py:239). Omitting it silently downgrades the catalog's L4/UDP
    entries (visio, wireguard) to HTTP, so their snapshots would place UDP
    ports behind nginx, leave instance_l4_* empty, and never exercise the L4
    render path at all."""
    meta_path = version_dir / "metadata.json"
    if not meta_path.is_file():
        return {}
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return {}
    ports = {}
    for entry in meta.get("ports", []) or []:
        name = entry.get("name")
        if not name:
            continue
        ports[name] = {
            "exposure_tier": entry.get("exposure_tier", "http"),
            "protocol": entry.get("protocol", "tcp"),
            "same_port": bool(entry.get("same_port", False)),
        }
    return ports


def _configurations_for(version_dir: pathlib.Path):
    """Feed each config its catalog default, the way a freshly-created
    instance does before the user edits anything."""
    meta_path = version_dir / "metadata.json"
    if not meta_path.is_file():
        return []
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return []
    configs = []
    for entry in meta.get("configurations", []) or []:
        configs.append({
            "value": entry.get("default_value") or {},
            "destinations": entry.get("destinations") or [],
        })
    return configs


def _greffon_info(instance_id: str, version_dir: pathlib.Path):
    """The START-REQUEST shape (what the manager POSTs), not the derived
    greffon_info: get_greffon_info builds the ports/volumes/networks itself,
    which is the point of routing through it."""
    return {
        "id": instance_id,
        "cert": {"certificate": "-----BEGIN CERTIFICATE-----\nsnapshot\n"
                                "-----END CERTIFICATE-----\n",
                 "private_key": "-----BEGIN PRIVATE KEY-----\nsnapshot\n"
                                "-----END PRIVATE KEY-----\n"},
        "configurations": _configurations_for(version_dir),
        "ports": _ports_for(version_dir),
        # Field names are the manager's contract (SMTPConfigSerializer in
        # apps/integrations/types/smtp.py): host, port, username, password,
        # from_address, tls_mode. Getting this wrong is not cosmetic -- 15
        # catalog composes reference {{ smtp.username }}, and a fixture that
        # spells it "user" renders them EMPTY, so the snapshots would bless an
        # impossible configured-SMTP state and never exercise interpolation.
        "integrations": {
            "smtp": {
                "host": "smtp.example.test",
                "port": 587,
                "username": "mailer@example.test",
                "password": "pw",
                "from_address": "noreply@example.test",
                "tls_mode": "starttls",
            },
        },
    }


def _render(compose_path: pathlib.Path, tmp_path: pathlib.Path) -> str:
    """Drive the REAL start-flow render and return the rendered compose, or a
    stable marker describing how it failed.

    The order here mirrors app/routers/controller.py exactly:
    get_greffon_info -> build_render_context -> get_compose_template ->
    apply_configuration -> create_compose. Calling create_compose alone would
    snapshot raw catalog ports and volumes with no greffon_nginx sidecar and
    no config destinations applied -- i.e. not the bytes a deployment actually
    renders, so a regression in the real input could pass this gate.

    Only host-port allocation is stubbed, because it probes real sockets and a
    snapshot that moves on its own proves nothing. Everything else is
    production code. A render failure comes back as a ``<<...>>`` marker, and
    the caller refuses it unless _KNOWN_UNRENDERABLE names the entry."""
    raw = compose_path.read_text()
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return f"<<UNPARSEABLE {type(exc).__name__}>>"
    if not isinstance(parsed, dict) or "services" not in parsed:
        return "<<NO SERVICES KEY>>"

    instance_id = "regress-" + compose_path.parent.parent.name.replace("_", "-")
    info_seed = _greffon_info(instance_id, compose_path.parent)

    prev = os.environ.get("GREFFON_PATH")
    os.environ["GREFFON_PATH"] = str(tmp_path)
    try:
        # Two stubs, both for determinism rather than convenience:
        # get_free_ports probes real sockets, and the L4 reservation asks the
        # docker daemon which host ports are occupied. Neither answer is
        # stable, and a snapshot that moves on its own proves nothing. The
        # allocation LOGIC (sticky reuse, same_port pinning, per-protocol
        # namespacing) still runs for real against an empty host.
        with mock.patch(
            "apps.utils.greffon.repository.get_free_ports",
            side_effect=lambda host="127.0.0.1", numbers=1, protocol="tcp": (
                list(range(20000, 20000 + numbers))),
        ), mock.patch(
            "apps.utils.docker.l4_ports.published_l4_ports", return_value={},
        ), mock.patch(
            "apps.utils.docker.l4_ports.pending_and_prune", return_value={},
        ), mock.patch(
            # mark_pending records the reservation in a MODULE-GLOBAL set that
            # outlives this test. Without stubbing it, rendering visio and
            # wireguard leaves udp/20000 reserved, and
            # test_l4_network_exposure.py (same pytest process) then allocates
            # 20001 while asserting 20000 -- so the oracle would turn the whole
            # suite red, including the release gate. Same class of bug as a
            # leaked env var: a test that mutates global state for everyone
            # after it.
            "apps.utils.docker.l4_ports.mark_pending",
        ):
            info = repository.get_greffon_info(parsed, info_seed)
        # The manager assigns each port its public URL and sends it in the
        # start request; get_greffon_info leaves url=None without it, and
        # instance_url is derived from ports[0].url. Assign deterministically
        # so the snapshot pins real URL interpolation rather than a fallback.
        for idx, port in enumerate(info.get("ports", [])):
            if port.get("exposure_tier") != "l4":
                port["url"] = f"https://{instance_id}-{idx}.my.example.test"
        compose_mod.build_render_context(info)
        template = compose_mod.get_compose_template(parsed, info)
        compose_mod.apply_configuration(info, parsed)
        compose_mod.create_compose(template, info)
    except Exception as exc:  # noqa: BLE001 -- pinning today's failure modes
        return f"<<RENDER FAILED {type(exc).__name__}>>"
    finally:
        # Never leak GREFFON_PATH out of this helper: the suite shares one
        # process and test_settings.py asserts the unset default.
        if prev is None:
            os.environ.pop("GREFFON_PATH", None)
        else:
            os.environ["GREFFON_PATH"] = prev
    written = tmp_path / instance_id / "docker-compose.yml"
    if not written.is_file():
        return "<<NO OUTPUT WRITTEN>>"
    return written.read_text()


@pytest.mark.skipif(not _ENTRIES, reason="greffon-catalog checkout not found")
@pytest.mark.parametrize("name,compose_path", _ENTRIES,
                         ids=[n for n, _ in _ENTRIES])
def test_catalog_entry_render_is_unchanged(name, compose_path, tmp_path):
    if not _UPDATE and _ACTUAL_SHA != _PINNED_SHA:
        # Reported once, loudly, by test_the_oracle_actually_covers_the_catalog,
        # which never skips on this. Comparing here would turn catalog drift
        # into one misleading "render changed" failure per entry, each pointing
        # at the regenerate command -- the one fix that breaks CI.
        pytest.skip("catalog under test is not the pinned commit")
    rendered = _render(compose_path, tmp_path)
    # Before the snapshot is read OR written: regenerating must not bless it.
    problem = _render_problem(name, rendered, _KNOWN_UNRENDERABLE)
    if problem:
        pytest.fail(problem)
    snap = _SNAP_DIR / (name.replace("/", "__") + ".snap")
    if _UPDATE:
        if _ACTUAL_SHA is None:
            pytest.fail(
                "cannot read the catalog commit, so refusing to write snapshots "
                "nothing could trace back to a catalog version. Point "
                "GREFFON_CATALOG_DIR at a git checkout of the catalog.")
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(rendered)
        _SHA_FILE.write_text(_ACTUAL_SHA + "\n")
        pytest.skip(f"snapshot written for {name} at catalog {_ACTUAL_SHA[:12]}")
    if not snap.is_file():
        pytest.fail(
            f"no snapshot for {name}. Generate with "
            f"CATALOG_RENDER_UPDATE=1 and review the diff before committing.")
    assert rendered == snap.read_text(), (
        f"render changed for {name}. If this change is intended, regenerate "
        f"with CATALOG_RENDER_UPDATE=1 and review every moved byte.")


def test_the_oracle_actually_covers_the_catalog():
    """A harness that silently covers nothing is worse than none: it reads as
    proof.

    This test deliberately does NOT skip when the catalog is missing. A skip
    here is indistinguishable from green, and the failure mode it would hide is
    the whole suite gating nothing -- which is exactly what happens in a CI job
    that checks out only the greffer repo. Set GREFFON_CATALOG_OPTIONAL=1 to
    opt out deliberately, and point GREFFON_CATALOG_DIR at a pinned catalog
    checkout in CI."""
    # Regenerating is how the commit legitimately changes, so the commit is
    # not checked in that mode; everything else still is.
    problem = _coverage_problem(_ENTRIES, _ACTUAL_SHA, _PINNED_SHA,
                                check_sha=not _UPDATE)
    if problem and os.getenv("GREFFON_CATALOG_OPTIONAL") == "1":
        pytest.skip(f"accepted as optional: {problem}")
    assert problem is None, problem


# Each refusal above, seen to fire. _coverage_problem and _render_problem run
# against the real catalog in CI, where every entry renders and the commit
# matches, so without these nothing would ever exercise the branches that
# exist to say no.
_SHA_A, _SHA_B = "a" * 40, "b" * 40
_FULL = [(f"g{i}/1.0", None) for i in range(_MIN_ENTRIES + 4)]


def test_the_coverage_floor_rejects_a_partial_catalog():
    problem = _coverage_problem(_FULL[:3], _SHA_A, _SHA_A)
    assert problem and "only 3" in problem


def test_a_catalog_at_another_commit_is_refused_not_compared():
    problem = _coverage_problem(_FULL, _SHA_B, _SHA_A)
    assert problem and _SHA_A in problem and _SHA_B[:12] in problem


def test_an_unreadable_catalog_commit_is_refused():
    problem = _coverage_problem(_FULL, None, _SHA_A)
    assert problem and "unreadable" in problem


def test_the_pinned_catalog_at_its_commit_is_accepted():
    assert _coverage_problem(_FULL, _SHA_A, _SHA_A) is None


def test_a_failed_render_is_a_failure_not_a_snapshot():
    problem = _render_problem("x/1.0", "<<RENDER FAILED SecurityError>>", {})
    assert problem and "does not render" in problem


def test_a_known_unrenderable_entry_is_accepted_with_its_reason():
    assert _render_problem("x/1.0", "<<RENDER FAILED X>>", {"x/1.0": "why"}) is None


def test_a_known_unrenderable_entry_that_renders_must_leave_the_list():
    problem = _render_problem("x/1.0", "services: {}\n", {"x/1.0": "why"})
    assert problem and "renders now" in problem


def test_a_directory_inside_another_repo_has_no_catalog_commit():
    # A plain tree nested in the greffer checkout would otherwise report the
    # GREFFER's HEAD as the catalog's commit.
    nested = _HERE / "snapshots"
    assert _catalog_sha(nested) is None


def test_ci_checks_out_the_catalog_the_snapshots_record():
    """Both workflows that run this oracle must pin the commit CATALOG_SHA
    names. Bump a workflow without regenerating, or regenerate without bumping
    the workflows, and CI compares one catalog against another's snapshots."""
    assert _PINNED_SHA is not None, f"{_SHA_FILE} is missing"
    pins = {}
    for workflow in ("ci.yml", "docker-publish.yml"):
        text = (_WORKFLOWS / workflow).read_text()
        match = re.search(
            r"repository:\s*greffon/greffon-catalog\s*\n\s*ref:\s*([0-9a-f]{40})",
            text)
        assert match, f"{workflow} no longer checks out a pinned greffon-catalog"
        pins[workflow] = match.group(1)
    assert set(pins.values()) == {_PINNED_SHA}, (pins, _PINNED_SHA)
