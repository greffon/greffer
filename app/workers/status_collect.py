"""Shared greffon-instance status collection (greffer-observability epic).

The monitor (per-transition callbacks) and the heartbeat (full liveness map)
both need ``{greffon_id: compose-status}`` for every instance under
``$GREFFON_PATH``. Extracting it into one collector lets the heartbeat reuse the
monitor's most recent sweep (``app.state.status_map``) so the two timers don't
each hit docker.
"""
from __future__ import annotations

import os

from app.settings import Settings


def collect_status_map(settings: Settings) -> dict[str, str]:
    """Return ``{greffon_id: status}`` for all instances under GREFFON_PATH.

    Dotfiles are skipped: internal state (``.greffer-token``, the
    ``.greffer-migrations.*`` markers) lives under GREFFON_PATH too and is not a
    greffon instance. UUID instance ids never start with a dot. ``status`` is
    one of ``running`` / ``stopped`` / ``unknow`` (see ``compose.get_status``).
    """
    # Imported lazily so unit tests can mock before the docker SDK initializes
    # its from_env() client at import (mirrors the monitor's lazy import).
    from apps.utils.docker import compose

    result: dict[str, str] = {}
    for greffon_id in os.listdir(str(settings.greffon_path)):
        if greffon_id.startswith("."):
            continue
        result[greffon_id] = compose.get_status(greffon_id)["status"]
    return result
