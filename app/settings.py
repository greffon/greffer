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
    # so feature #3 ships dormant alongside the still-live Django runtime.
    # Feature #4's cutover PR flips this to True in the new compose config
    # at the same moment the Django entrypoint is removed. Enabling this
    # while Django is also running causes double-registration and races
    # over the nginx cert files inside the DOCKER_NGINX_NAME container.
    workers_enabled: bool = False

    logger_name: str = "greffer"


@lru_cache
def get_settings() -> Settings:
    return Settings()
