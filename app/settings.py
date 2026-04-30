from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    greffer_id: str

    # Optional token override; primarily for tests and operator-driven
    # explicit rotation. When unset, ``create_app`` mints a fresh random
    # token each process. Sibling services (e.g. tunnel-sidecar) read the
    # active token via the file at ``greffer_token_file_path`` — see
    # ``app/lifespan.py`` for the write side.
    greffer_token: str | None = None

    # Optional mode declaration, included in the register payload so the
    # manager can validate the greffer-side intent matches the stored
    # ``Greffer.mode``. Operators flipping a registered greffer between
    # proxy ↔ tunnel via ``PATCH /api/greffer/{id}/mode/`` MUST also set
    # this on the greffer host (env.env: ``GREFFER_MODE=tunnel``) and
    # restart the greffer; otherwise the next register call sends no
    # mode (defaulting to ``proxy`` server-side) and 400s with
    # ``mode_mismatch`` against the new stored value, leaving the greffer
    # stuck on its old cert. Surfaced by the QA on 2026-04-30.
    greffer_mode: Literal["proxy", "tunnel"] | None = None

    @field_validator("greffer_mode", mode="before")
    @classmethod
    def _empty_string_is_none(cls, v):
        # env.env documents an empty default ``GREFFER_MODE=`` for the
        # common case where operators haven't opted into tunnel mode.
        # Without this validator, pydantic-settings would feed the
        # empty string into the Literal validation and fail.
        # Scope: this field only — a model-wide ``env_ignore_empty=True``
        # would silently turn empty values into defaults for fields
        # whose contract is "empty disables" (e.g. greffer_token_file_path).
        # Codex P2 on greffer#23.
        if isinstance(v, str) and v == "":
            return None
        return v

    # Where ``app/lifespan.py`` writes the active token on startup so the
    # tunnel-sidecar can authenticate against the manager with the same
    # ``X-GREFFON-TOKEN``. The compose tunnel profile mounts this path
    # as a shared volume between greffer and sidecar. Empty disables.
    greffer_token_file_path: str = "/run/tunnel-secrets/greffer-token"

    greffon_base_server: str = "https://api.greffon.io"
    greffer_protocol: Literal["http", "https"] = "https"
    greffer_ssl_verify: bool = True
    greffer_address: str | None = None
    greffer_port: int = 8000

    greffer_public_host: str = "host.docker.internal"
    greffer_public_scheme: Literal["http", "https"] = "https"

    greffon_path: Path = Path("/data")

    docker_nginx_name: str = "greffer-nginx-1"

    crl_sync_interval: int = 300
    monitor_interval: int = 5

    skip_ops_migrations: bool = False

    # Workers (register / monitor_status / CRL sync). Disabled by default
    # so unit tests don't accidentally start real workers. Production
    # enables via ``GREFFER_WORKERS_ENABLED=true`` in compose.
    #
    # NOTE: the field name must carry the ``greffer_`` prefix because
    # pydantic-settings maps field → env var by field name (case-
    # insensitive), not via an env_prefix config. A bare
    # ``workers_enabled`` would silently bind to ``WORKERS_ENABLED``,
    # ignoring ``GREFFER_WORKERS_ENABLED`` entirely — a cutover-blocking
    # bug Codex caught before merge (greffon/greffer#17 review).
    greffer_workers_enabled: bool = False

    logger_name: str = "greffer"


@lru_cache
def get_settings() -> Settings:
    return Settings()
