"""Where the CLI writes its on-disk state.

Default: ``~/.greffer/`` on POSIX, ``%LOCALAPPDATA%\\greffer\\`` on
Windows. Honor ``XDG_CONFIG_HOME`` on Linux when set.

Operator override: ``--config-dir <path>`` (for sysadmins installing
greffers for a service account).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_config_dir() -> Path:
    """Resolve the default ``~/.greffer/`` location for this host."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "greffer"
        # Fallback: %USERPROFILE%\AppData\Local\greffer
        return Path.home() / "AppData" / "Local" / "greffer"

    # POSIX: honor XDG_CONFIG_HOME on Linux; fall back to ~/.greffer/
    # on macOS (which doesn't use XDG) and as last resort on Linux.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and sys.platform.startswith("linux"):
        return Path(xdg) / "greffer"
    return Path.home() / ".greffer"


def resolve_config_dir(override: str | None = None) -> Path:
    """Return the config directory, honoring an explicit ``--config-dir``."""
    if override:
        return Path(override).expanduser().resolve()
    return default_config_dir()


def env_env_path(config_dir: Path) -> Path:
    return config_dir / "env.env"


def docker_compose_yml_path(config_dir: Path) -> Path:
    return config_dir / "docker-compose.yml"
