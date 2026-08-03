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

    def run(self) -> dict:
        root = os.getcwd()
        removed = errors = 0
        keys = certs = 0
        try:
            names = os.listdir(root)
        except OSError as exc:
            logger.warning("0002: cannot list %s: %s", root, exc)
            return {"migrated": 0, "skipped": 0, "errors": 1, "backups": []}

        for name in names:
            if not _UUID_NAME.match(name):
                continue
            path = os.path.join(root, name)
            if not _is_stray(path):
                continue
            # Record what it was before unlinking, so the summary tells an
            # operator whether key material was actually exposed here.
            try:
                with open(path, "rb") as f:
                    head = f.read(40)
                if b"PRIVATE KEY" in head:
                    keys += 1
                else:
                    certs += 1
                os.unlink(path)
                removed += 1
            except OSError as exc:
                logger.warning("0002: failed to remove %s: %s", path, exc)
                errors += 1

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
            "skipped": 0,
            "errors": errors,
            "backups": [],
            "private_keys_removed": keys,
            "certificates_removed": certs,
        }
