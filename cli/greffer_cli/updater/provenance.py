"""Image-provenance verification (cosign) for the v2 updater.

Before recreate, every ``greffon/*`` image is proven genuine: resolved to a
digest, cosign-verified against the baked public key AND bound to its expected
repo, then pinned by that digest. Acting on the digest (not the mutable tag)
closes the tag-moved TOCTOU; the repo binding closes the same-key cross-image
substitution (key-only cosign proves a Greffon key signed *some* digest, not
which repo). Fail-closed: any error returns None / False.

cosign and docker are invoked via ``greffer_cli.compose._run`` so tests can
monkeypatch the subprocess layer without a real registry.
"""

from __future__ import annotations

from .. import compose

# Baked into the greffer-updater image at build time (the publish CI commits
# cosign.pub into the image); the updater holds what it needs to verify offline.
DEFAULT_COSIGN_PUB = "/etc/greffer/cosign.pub"

# The OCI label the publish CI stamps with each image's build version, so the
# floor check reads the actual version from the verified image, not the tag.
VERSION_LABEL = "org.opencontainers.image.version"


def resolve_digest(ref: str) -> str | None:
    """Resolve ``greffon/<repo>:<tag>`` to its content digest (``sha256:<64hex>``),
    or None. Uses ``docker buildx imagetools inspect`` so it needs no pull."""
    res = compose._run(
        ["docker", "buildx", "imagetools", "inspect", ref,
         "--format", "{{.Manifest.Digest}}"],
        timeout=60,
    )
    if not res.ok:
        return None
    digest = res.stdout.strip()
    if not digest.startswith("sha256:"):
        return None
    hexpart = digest[len("sha256:"):]
    return digest if len(hexpart) == 64 and all(
        c in "0123456789abcdef" for c in hexpart
    ) else None


def cosign_verify(repo: str, digest: str, *, pubkey: str = DEFAULT_COSIGN_PUB) -> bool:
    """True iff ``greffon/<repo>@<digest>`` carries a valid Greffon signature
    **bound to that repo**. The ``repo`` annotation (set by the publish CI) is
    required so a signature lifted from another Greffon image (an old below-floor
    nginx, or the updater) cannot pass in this slot. Fail-closed on any error."""
    ref = f"{repo}@{digest}"
    # ``--insecure-ignore-tlog=true``: the trust model is an offline managed key
    # (cosign verify --key), NOT keyless+Rekor (see the v2 trust-model doc,
    # "Signing mechanism"). The publish CI signs with ``--tlog-upload=false``, so
    # there is no transparency-log entry; without this flag cosign v2 defaults to
    # REQUIRING one and the verify fails closed on every genuine image. The repo
    # annotation, not a tlog identity, binds the signature to its slot.
    # ``-a repo=<repo>`` (short for ``--annotations``) requires the signature to
    # carry that annotation; the publish CI signs each image with the same flag.
    # Short form on BOTH sides avoids the invalid singular ``--annotation`` (cosign
    # only defines ``-a`` / ``--annotations``), and the release pipeline runs this
    # exact verify as a sign->verify smoke test so a flag drift fails CI, not prod.
    res = compose._run(
        ["cosign", "verify", "--key", pubkey,
         "-a", f"repo={repo}",
         "--insecure-ignore-tlog=true", ref],
        timeout=120,
    )
    return res.ok


def image_version(ref_or_digest: str, *, label: str = VERSION_LABEL) -> str | None:
    """Read the build version from a (verified, locally-present) image's OCI
    ``org.opencontainers.image.version`` label via ``docker inspect``; None if
    absent. Read from the image, never the tag string, so a non-semver tag
    (``latest``) cannot spoof the version the floor compares."""
    res = compose._run(
        ["docker", "inspect", "--format",
         f'{{{{ index .Config.Labels "{label}" }}}}', ref_or_digest],
        timeout=30,
    )
    if not res.ok:
        return None
    value = res.stdout.strip()
    # docker prints "<no value>" for an absent label.
    return value if value and value != "<no value>" else None
