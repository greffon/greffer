"""0002 — delete TLS key material left in the working directory by the old
volume-staging code.

Pairs with the fix in `apps/utils/docker/volume.py`. Before it,
`docker_copy_file_into_volume` staged every ``type: 'content'`` file at a bare
``uuid4()`` RELATIVE path and never removed it. The greffer runs with
``WORKDIR /app`` and compose bind-mounts ``./:/app``, so those landed in the
operator's greffer checkout on the host — and the staged content includes each
instance's UNENCRYPTED TLS private key (``cert.key``).

Every greffon start therefore left a private key behind. On the checkout this
was found in: 218 files, 109 of them private keys.

The code fix stops new ones appearing and a ``.gitignore`` rule stops one ever
being committed. Neither removes what is already there — and the ignore rule
makes them invisible to ``git status``, so without this migration the fix
converts a visible mess into a silent one. This runs at boot, before uvicorn
binds, on every greffer.

Marked ``advisory`` (see ``Migration.advisory``): it re-scans on EVERY boot
rather than once, and a failure is logged loudly instead of gating startup.
Both matter here. Strays can appear after the first run (a restored data root,
a first boot without the bind mount) and the ``.gitignore`` rule now hides them
from ``git status``, so a one-shot purge would silently skip the very thing it
exists to remove. And crashlooping a greffer because one duplicate file could
not be unlinked trades a whole node's availability for nothing.

Destructive by necessity: the whole point is that this material must not
persist. It is safe because the files are pure duplicates — the volume already
holds its own copy, which is what nginx actually serves. Deleting them cannot
affect a running instance.

Narrow by construction, because it deletes: a candidate must sit at the CWD
root (never a subdirectory), match the exact 36-character UUID shape with no
extension, be a regular file, AND begin with a PEM header. Anything failing any
one of those is left alone.
"""
from __future__ import annotations

import logging
import os
import re

from ..base import Migration
from ..registry import register

logger = logging.getLogger("greffer.ops_migrations")

# The exact shape the old code produced: str(uuid4()), nothing else.
_UUID_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PEM_PREFIX = b"-----BEGIN "


def _is_stray(path: str) -> bool:
    """A file is a stray only if it is BOTH uuid4-named and PEM-headed.

    The content sniff is what makes this safe to run unattended: a
    coincidentally uuid-named file that is not PEM is never touched.
    """
    try:
        if not os.path.isfile(path) or os.path.islink(path):
            return False
        with open(path, "rb") as f:
            return f.read(len(_PEM_PREFIX)) == _PEM_PREFIX
    except OSError:
        return False


@register
class PurgeStagedKeyStrays(Migration):
    id = "0002_purge_staged_key_strays"
    description = (
        "Delete uuid4-named PEM files left in the working directory by the "
        "pre-fix volume-staging code (unencrypted instance TLS private keys)."
    )

    # Cleanup, not a state change the runtime depends on. Without this flag a
    # single un-deletable stray (a read-only mount, a hardening `USER`, an
    # immutable flag) returns errors>0, the runner withholds `applied`, the CLI
    # exits 1, and the `&&`-gated CMD never starts uvicorn -- so the greffer
    # CRASHLOOPS forever to avoid leaving one duplicate file on disk. The volume
    # already holds the copy nginx serves; that trade is backwards.
    advisory = True

    def run(self, data_root: str) -> dict:
        # ``data_root`` is required by the runner's contract but deliberately
        # unused: the strays are NOT under $GREFFON_PATH. The old code wrote a
        # bare relative filename, so they resolved against the process CWD,
        # which in a bind-mounted dev checkout is the repo root.
        del data_root
        root = os.getcwd()
        removed = errors = skipped = 0
        keys = certs = 0
        try:
            names = os.listdir(root)
        except OSError as exc:
            logger.warning("0002: cannot list %s: %s", root, exc)
            return {"migrated": 0, "skipped": 0, "errors": 1, "backups": []}

        for name in names:
            # A non-uuid name is NOT counted in `skipped`: the CWD is the whole
            # greffer checkout, so counting every unrelated entry would report
            # ~28 on a clean node and tell an operator nothing. `skipped` means
            # uuid-NAMED candidates the content sniff deliberately spared --
            # the number that shows the narrow filter actually engaged.
            if not _UUID_NAME.match(name):
                continue
            path = os.path.join(root, name)
            if not _is_stray(path):
                skipped += 1
                continue
            # Record what it was before unlinking, so the summary tells an
            # operator whether key material was actually exposed here.
            try:
                with open(path, "rb") as f:
                    head = f.read(40)
                was_key = b"PRIVATE KEY" in head
                os.unlink(path)
            except FileNotFoundError:
                # Already gone (a concurrent operator cleanup, or a second
                # greffer sharing this bind-mounted CWD -- the runner flock is
                # scoped to $GREFFON_PATH and does not exclude them). The goal
                # state is reached, so this is NOT an error: counting it would
                # fire the "key material may still be exposed" CRITICAL for a
                # file that no longer exists.
                skipped += 1
                continue
            except OSError as exc:
                logger.warning("0002: failed to remove %s: %s", path, exc)
                errors += 1
                continue
            # Count ONLY after a successful unlink. Incrementing at
            # classification time reported a file that FAILED to unlink as
            # removed key material -- and this summary is the operator's signal
            # for whether a key is still exposed, so an optimistic count there
            # is worse than no count at all.
            if was_key:
                keys += 1
            else:
                certs += 1
            removed += 1

        if errors:
            # CRITICAL, not warning: this is the one outcome where key material
            # the migration was written to destroy is still sitting on disk.
            # Advisory means boot continues, so this log is the ONLY signal.
            logger.critical(
                "0002: FAILED to remove %d staged TLS file(s) from %s — key "
                "material may still be exposed there. Boot continues (advisory) "
                "and this retries on the next start; remove them by hand if it "
                "persists.",
                errors, root,
            )
        if removed:
            logger.warning(
                "0002: removed %d staged TLS file(s) from %s (%d private "
                "key(s), %d certificate(s)). These were written by the "
                "pre-fix volume-staging code. Any leaked key belongs to a "
                "manager-CA-signed instance cert; rotating those instances "
                "(a restart re-mints) is the conservative follow-up.",
                removed, root, keys, certs,
            )
        return {
            "migrated": removed,
            "skipped": skipped,
            "errors": errors,
            "backups": [],
            "private_keys_removed": keys,
            "certificates_removed": certs,
        }
