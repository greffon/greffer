"""Stable, persisted greffer registration token.

The token authenticates the greffer to the manager: it is sent as
``X-Greffer-Token`` on the cert poll (which hands back the greffer's private
key) and stamped into the register payload. The manager treats possession of
this token as the greffer's identity proof — so it MUST stay the same across
container restarts. A reported address is a container IP and changes on every
recreation; the token is what lets the manager recognise a restarted greffer
as the same claimant rather than a hijacker (see manager ``register`` view).

A fresh random token per process would force the manager to treat each restart
as a new claimant and, on an IP change, reject the re-register with
``greffer_id_claimed``. So we mint once and persist to the greffer's data
volume, then reuse it on every subsequent boot.
"""
from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path

logger = logging.getLogger("greffer")

# Owner read/write only. The token is a bearer secret on a volume that may be
# shared with sibling containers (rathole-client), so it must not be
# world-readable — same posture as the mTLS private key already stored there.
_TOKEN_FILE_MODE = 0o600
_TOKEN_BYTES = 32


def load_or_create_token(path: Path) -> str:
    """Return the persisted greffer token at ``path``, minting+persisting one
    on first boot.

    Resolution:
      - file exists and is non-empty -> return its contents (stable identity).
      - otherwise -> mint a random token, write it atomically, return it.

    Persistence is best-effort: if the file can't be read or written (e.g. the
    volume is mounted read-only, or a permissions problem), fall back to an
    in-memory random token for this process rather than refusing to boot. The
    next boot retries persistence. The cost of the fallback is that this one
    process looks like a new claimant to the manager — degraded, not broken.
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        # A read failure on an existing token (perms/IO) is a misconfig, not a
        # routine condition — log at ERROR so it's not lost in normal volume.
        logger.error(
            "could not read greffer token file %s (%s); using an ephemeral "
            "token for this process. The greffer will look like a NEW claimant "
            "to the manager and may be rejected (greffer_id_claimed) on an IP "
            "change until this is fixed.",
            path, exc,
        )
        return _mint()
    if existing:
        return existing

    token = _mint()
    try:
        _atomic_write(path, token)
    except OSError as exc:
        logger.error(
            "could not persist greffer token to %s (%s); using an ephemeral "
            "token for this process. The greffer will look like a NEW claimant "
            "to the manager and may be rejected (greffer_id_claimed) on an IP "
            "change until persistence succeeds. Check that GREFFON_PATH is a "
            "writable, persistent volume.",
            path, exc,
        )
        return token
    logger.info("minted and persisted a new greffer token at %s", path)
    return token


def _mint() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically so a crash mid-write never
    leaves a half-written (and thus identity-changing) token.

    ``tempfile.mkstemp`` creates a uniquely-named file in the same directory
    with mode 0600 *at creation* — so the bearer token is never briefly
    world-readable (the old write_text+chmod left a 0644 window), and the
    unique name removes the temp-name collision/symlink risk. We then rename
    into place (atomic on the same filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # mkstemp already creates 0600; normalise explicitly in case of an
        # unusual prior umask interaction, before publishing.
        os.chmod(tmp, _TOKEN_FILE_MODE)
        os.replace(tmp, path)
    except OSError:
        # Don't leave a stray temp file behind on failure.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def resolve_token(settings) -> str:
    """Resolve the greffer's stable token the way ``create_app`` does, minus the
    test-only explicit kwarg: an operator-set ``GREFFER_TOKEN`` wins, otherwise
    the persisted on-disk token. Used on (re-)registration (greffer-observability
    epic) so a rotated on-disk token is picked up after a heartbeat 403."""
    return settings.greffer_token or load_or_create_token(
        settings.greffon_path / ".greffer-token")
