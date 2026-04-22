from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    greffer_id: str

    greffon_base_server: str = "https://api.greffon.io"
    greffer_protocol: Literal["http", "https"] = "https"
    greffer_address: str | None = None
    greffer_port: int = 8000

    # Local on-disk home for cert material pulled from the manager during
    # registration. This process reads from here to present a client cert
    # on outbound calls; the same files are also copied into the greffer's
    # nginx container so nginx can terminate TLS on inbound.
    greffer_cert_dir: Path = Path("/etc/greffer/certs")

    # Registration carries the greffer token and receives the signed
    # private key in the response body. If GREFFON_BASE_SERVER is not
    # https:// the process refuses to start, unless this opt-in is set.
    # Dev stacks that terminate TLS elsewhere (e.g. the root dev-proxy)
    # can enable it; prod must leave it off.
    greffer_allow_insecure_bootstrap: bool = False

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
