"""The v2 ``min_supported`` downgrade floor, with anti-replay.

A cosign-signed manifest proves authenticity, not freshness: a replayed older
manifest (or a stale-floored canary channel) could otherwise lower the floor
and re-admit a known-vulnerable but authentically-signed release. The EFFECTIVE
floor is ``max(baked build-time baseline, signed manifest min_supported,
persisted /data high-water ratchet)`` and only ever ratchets up. See the
trust-model doc, resolution (d) "Freshness".

Fail-closed: an unreachable / unverifiable / malformed manifest raises
``FloorError`` and the update aborts; the floor is never silently lowered.
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path

from .. import compose, update


class FloorError(Exception):
    """The floor could not be established. Fail-closed: abort the update."""


def parse_version(v: object) -> tuple[int, ...] | None:
    """'0.3.5' -> (0, 3, 5); None if not clean dotted-numeric."""
    if not isinstance(v, str):
        return None
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return None


def version_ge(a: object, b: object) -> bool:
    """a >= b, zero-padded. False if either is unparseable (fail-closed)."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return False
    n = max(len(pa), len(pb))
    return pa + (0,) * (n - len(pa)) >= pb + (0,) * (n - len(pb))


def higher(a: str | None, b: str | None) -> str | None:
    """The greater of two version strings; ignores an unparseable side."""
    if parse_version(a) is None:
        return b
    if parse_version(b) is None:
        return a
    return a if version_ge(a, b) else b


def _fetch_bytes(url: str, *, timeout: float = 10.0) -> bytes:
    """Bounded HTTPS-only fetch via the v1 hardened opener (refuses an
    https->http redirect downgrade). Raises FloorError fail-closed on failure."""
    if not url.startswith("https://"):
        raise FloorError(f"refusing non-https fetch: {url}")
    try:
        with update._MANIFEST_OPENER.open(url, timeout=timeout) as r:
            raw = r.read(update._MAX_MANIFEST_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FloorError(f"fetch failed ({url}): {exc}") from exc
    if len(raw) > update._MAX_MANIFEST_BYTES:
        raise FloorError(f"manifest oversized ({url})")
    return raw


def verify_blob(manifest_bytes: bytes, sig_bytes: bytes, *, cosign_pub: str) -> bool:
    """cosign verify-blob the manifest against its detached signature."""
    with tempfile.TemporaryDirectory() as d:
        mpath = Path(d) / "manifest.json"
        spath = Path(d) / "manifest.sig"
        mpath.write_bytes(manifest_bytes)
        spath.write_bytes(sig_bytes)
        res = compose._run(
            ["cosign", "verify-blob", "--key", cosign_pub,
             "--signature", str(spath), str(mpath)],
            timeout=120,
        )
    return res.ok


def signed_min_supported(manifest_url: str, *, cosign_pub: str) -> str | None:
    """Fetch the manifest + its ``.sig``, verify the signature, and return
    ``min_supported`` (None if the manifest carries none). Raises FloorError
    fail-closed on any fetch / signature / parse failure."""
    manifest_bytes = _fetch_bytes(manifest_url)
    sig_bytes = _fetch_bytes(manifest_url + ".sig")
    if not verify_blob(manifest_bytes, sig_bytes, cosign_pub=cosign_pub):
        raise FloorError("manifest signature did not verify")
    try:
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FloorError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FloorError("manifest is not a JSON object")
    ms = data.get("min_supported")
    return ms if isinstance(ms, str) else None


def read_ratchet(path: Path) -> str | None:
    """The persisted high-water floor on /data, or None if absent/garbage."""
    try:
        v = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v if parse_version(v) is not None else None


def write_ratchet(path: Path, version: str) -> None:
    """Persist the high-water floor (best-effort; a failed write still leaves
    the in-process floor enforced for this run)."""
    if parse_version(version) is None:
        return
    try:
        path.write_text(version + "\n", encoding="utf-8")
    except OSError:
        pass


def effective_floor(manifest_url: str, *, baked_baseline: str | None,
                    ratchet_path: Path, cosign_pub: str) -> str:
    """Compute and persist the effective floor =
    ``max(baked_baseline, signed manifest min_supported, persisted ratchet)``.

    Raises FloorError fail-closed if the signed manifest can't be established or
    no usable floor exists. Ratchets the high-water mark up, never down."""
    manifest_floor = signed_min_supported(manifest_url, cosign_pub=cosign_pub)
    prior = read_ratchet(ratchet_path)
    floor: str | None = None
    for candidate in (baked_baseline, manifest_floor, prior):
        floor = higher(floor, candidate)
    if floor is None or parse_version(floor) is None:
        raise FloorError(
            "no usable floor (no baked baseline, manifest min_supported, or ratchet)")
    write_ratchet(ratchet_path, floor)
    return floor
