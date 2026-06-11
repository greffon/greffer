from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    greffer_id: str

    # Optional token override; primarily for tests and operator-driven
    # explicit rotation. When unset, ``create_app`` loads (or mints + persists)
    # a STABLE token from the data volume (``resolve_token`` ->
    # ``load_or_create_token``), reused across restarts. Used as
    # ``X-Greffer-Token`` on the manager auth paths.
    greffer_token: str | None = None

    # Greffer software version, reported in the register payload. Defaults to
    # the worker's ``app.__version__``; overridable via ``GREFFER_VERSION`` (e.g.
    # a build/release stamp). The manager stamps ``Greffer.version`` from this
    # and uses it for the per-greffon ``min_greffer_version`` compat gate.
    greffer_version: str = __version__

    # Optional mode declaration, included in the register payload so the
    # manager can stamp ``Greffer.mode`` on first register or validate
    # against the stored value on re-register. v3 source-of-truth is this
    # env var: operators bring up a tunnel greffer by setting
    # ``GREFFER_MODE=tunnel`` here and starting compose; no admin pre-
    # configuration is needed.
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
        # whose contract is "empty disables" (e.g.
        # greffer_tunnel_client_config_path).
        # Codex P2 on greffer#23.
        if isinstance(v, str) and v == "":
            return None
        return v

    @field_validator("heartbeat_interval")
    @classmethod
    def _heartbeat_interval_in_range(cls, v):
        # Below 1 would busy-loop the worker; above the manager's cap (86400)
        # the manager 400s every beat. Match that contract and fail fast.
        if not 1 <= v <= 86400:
            raise ValueError("heartbeat_interval must be between 1 and 86400")
        return v

    # Where the greffer-side controller handler writes the rathole
    # ``client.toml`` pushed by the manager (in cert-poll responses,
    # start/stop request bodies). The compose tunnel profile mounts this
    # path as a shared volume between greffer and rathole-client; the
    # sidecar's file-watcher hot-reloads on change. Empty disables the
    # v3 push behaviour — the handler accepts ``tunnel_client_toml`` in
    # payloads but does not write it. (Useful in tests and in the
    # transitional step-1 deployment where a v2 manager isn't sending
    # the field at all.)
    greffer_tunnel_client_config_path: str = "/config/client.toml"

    greffon_base_server: str = "https://api.greffon.io"
    greffer_protocol: Literal["http", "https"] = "https"
    greffer_ssl_verify: bool = True
    greffer_address: str | None = None
    greffer_port: int = 8000

    greffer_public_host: str = "host.docker.internal"
    greffer_public_scheme: Literal["http", "https"] = "https"

    greffon_path: Path = Path("/data")

    # L4 (Tier-C) host ports are allocated from this dedicated range, NOT the OS
    # ephemeral range (ip_local_port_range, typically 32768-60999). A sticky L4
    # port that lives outside the ephemeral range can't be transiently stolen by
    # an outbound connection's source port while the instance is stopped, so the
    # endpoint stays stable across restarts (sticky allocation). Tier-A host
    # ports stay ephemeral (their host port is an internal nginx upstream, never
    # user-facing).
    greffer_l4_port_range_start: int = 20000
    greffer_l4_port_range_end: int = 29999

    docker_nginx_name: str = "greffer-nginx-1"

    crl_sync_interval: int = 300
    monitor_interval: int = 5
    # Heartbeat cadence (greffer-observability epic). Binds the unprefixed
    # HEARTBEAT_INTERVAL env, mirroring monitor_interval's MONITOR_INTERVAL
    # (not the greffer_ prefix — that pitfall applies only to fields whose
    # documented env var carries the prefix, e.g. greffer_workers_enabled).
    # The manager derives the unreachable threshold from this value.
    heartbeat_interval: int = 5

    # NOTE: the ops-migrations skip switch (GREFFER_SKIP_OPS_MIGRATIONS) is
    # intentionally NOT a Settings field. The runner reads it via os.getenv
    # because apps/utils/ops_migrations/ runs from the CLI entrypoint before
    # the app boots and stays import-independent of app.settings. A bare
    # ``skip_ops_migrations`` field here would also bind SKIP_OPS_MIGRATIONS,
    # not the documented env var (see prefix pitfall on greffer_workers_enabled
    # below).

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
