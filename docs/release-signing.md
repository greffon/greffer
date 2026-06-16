# Release signing (greffer self-update v2)

The v2 remote-update flow only trusts images and a version manifest signed by a
**managed cosign key pair**, verified **offline** on the greffer host (no Rekor /
Fulcio). This doc is the ops handoff: the pipeline code is in place
(`.github/workflows/docker-publish.yml`, `Dockerfile.updater`,
`min_supported_baseline`), but it stays dormant until the key material is
provisioned.

See the trust model: `docs/features/greffer-self-update/v2-image-provenance-and-trust-model.md`
in the greffon root repo.

## One-time: generate and provision the key

```sh
# Produces cosign.key (PRIVATE, encrypted with the prompt password) + cosign.pub.
COSIGN_PASSWORD='<a strong passphrase>' cosign generate-key-pair
```

Then in the greffer repo's GitHub settings:

| Name                   | Kind                | Value                          |
|------------------------|---------------------|--------------------------------|
| `COSIGN_PRIVATE_KEY`   | Actions **secret**  | full contents of `cosign.key`  |
| `COSIGN_PASSWORD`      | Actions **secret**  | the passphrase above           |
| `COSIGN_PUBLIC_KEY`    | Actions **variable**| full contents of `cosign.pub`  |

The public key is a **variable** (not a secret): it is baked into the
`greffer-updater` image at build time and is not sensitive. Keep `cosign.key`
offline; losing it means rotating the key (see below). Once these are set, the
next push to `main` signs every `greffon/*` image, builds + signs
`greffon/greffer-updater`, and round-trip-verifies each with the updater's exact
verify command plus a version-label read (the workflow fails the release on any
cosign drift). That round-trip is also what enforces that `COSIGN_PUBLIC_KEY`
actually pairs with `COSIGN_PRIVATE_KEY`: a mismatched pair signs but fails the
verify, so a paste error is caught at release time, not on a host.

## Per host: opt in and pin the updater digest

Remote update is operator-sovereign and **off by default**. On each greffer host
that should accept manager-triggered updates, set in `env.env`:

```sh
GREFFER_REMOTE_UPDATE_ENABLED=true
# Pin the updater by DIGEST (never a mutable tag): it is the one root-equivalent,
# socket-mounted container, so a moved tag must not be able to swap it.
GREFFER_UPDATER_IMAGE=greffon/greffer-updater@sha256:<digest>
```

Resolve the digest for a published version with:

```sh
docker buildx imagetools inspect greffon/greffer-updater:<version> \
  --format '{{.Manifest.Digest}}'
```

## Per release: sign and publish the version manifest

The greffer fetches the version manifest (`GREFFER_VERSION_MANIFEST_URL`, default
`https://greffon.io/greffer-version.json`) **and its detached signature** at
`<url>.sig`, then `cosign verify-blob`s it before trusting `min_supported` /
`latest`. Sign with the SAME flags the host verifies with (no tlog):

```sh
COSIGN_PASSWORD='…' cosign sign-blob --key cosign.key --tlog-upload=false \
  --output-signature greffer-version.json.sig greffer-version.json
```

Publish **both** `greffer-version.json` and `greffer-version.json.sig` at the
manifest URL (this lives in the landing-page / `greffon.io` hosting, not this
repo). The signature filename MUST be the manifest URL path with `.sig`
appended: the host fetches `<GREFFER_VERSION_MANIFEST_URL>.sig` literally
(`floor.signed_min_supported`). It then runs `cosign verify-blob --key
cosign.pub --signature <sig> --insecure-ignore-tlog=true <manifest>`.

## The `min_supported` baseline

`min_supported_baseline` (repo root, baked into the updater image) is the
build-time **downgrade floor**: the lowest version the updater will admit,
backstopping a replayed manifest and surviving a wiped `/data`. Raise it in the
same PR that ships a release fixing a security issue, so a node can never be
updated back onto the vulnerable line. The effective floor is
`max(this baseline, signed manifest min_supported, persisted /data ratchet)` and
only ever ratchets up.

## Key rotation

The managed-key design has no per-image revocation (raising the floor is how a
bad version is retired). To rotate the signing key: generate a new pair, update
the three CI entries above, re-publish (re-sign) the manifest with the new key,
and roll a new `greffer-updater` image baking the new `cosign.pub`. Hosts pick up
the new public key when their pinned `GREFFER_UPDATER_IMAGE` digest is bumped
(trust-on-first-use, same as the install path).
