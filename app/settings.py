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

    # Backup destination (backup-restore Phase 1). The greffer holds the restic
    # repo + creds, so the manager never touches bytes. One repo per greffer;
    # snapshots tagged ``instance:<id>``, ``--host <greffer-id>``. Unset => the
    # backup endpoints fail with ``repo_uninitialized``. Env: GREFFER_BACKUP_REPO,
    # RESTIC_PASSWORD(_FILE), AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY.
    greffer_backup_repo: str | None = None
    restic_password: str | None = None
    restic_password_file: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    # Digest-pinned restic sidecar image (like the updater image). MUST be >= 0.17
    # -- restore_instance uses `restore --delete` (clean overwrite: remove target
    # files absent from the snapshot), a flag that only landed in restic 0.17, so
    # the prior 0.16.4 pin made RESTORE FAIL ("unknown flag: --delete"). The digest
    # is the multi-arch manifest-list digest for 0.17.3 (platform-independent).
    restic_sidecar_image: str = (
        "restic/restic:0.17.3@sha256:"
        "8f5a62b422a2cb1277ea0dd6e826fe1acf649e5b9f02d60e5268d5fd1976255a")
    # Max wait for `docker compose stop` to quiesce before a backup snapshots.
    backup_stop_timeout_seconds: int = 120
    # restic retention (Feature #5). `forget` runs after each backup, tag-isolated
    # to instance:<id>; the safety:<id> snapshots get their OWN bounded keep-last
    # so they neither accumulate forever nor are swept by the instance policy.
    # Env defaults; a manager BackupPolicy override may arrive in the request later.
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 4
    backup_keep_monthly: int = 6
    backup_safety_keep_last: int = 3
    # `forget` runs OFF the downtime-critical path with its own short timeout, so a
    # hung retention call never extends a backup/restore window.
    backup_forget_timeout_seconds: int = 300
    # prune (space reclaim) + check (integrity) are repo-wide and can be slow;
    # they run detached on their own cadence, so they get a generous timeout.
    backup_prune_timeout_seconds: int = 7200
    backup_check_timeout_seconds: int = 7200

    # Greffer software version, reported in the register payload and on every
    # heartbeat (see workers/heartbeat.py). Defaults to
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

    @field_validator("greffer_version")
    @classmethod
    def _truncate_version(cls, v):
        # The manager stores Greffer.version in a max_length=32 column and the
        # heartbeat serializer enforces 32, so an operator GREFFER_VERSION build
        # stamp longer than 32 would 400 every heartbeat (live greffer reads
        # unreachable). Truncate so a long stamp degrades to a valid value on
        # both the register and heartbeat paths.
        return v[:32] if v else v

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

    # Remote update toggle (greffer self-update v2). Default ON (product
    # decision 2026-06-17): the manager may trigger this greffer to update
    # itself. A manager-triggered update is root-equivalent, but the REAL gate is
    # cryptographic, not this flag: the controller route refuses unless
    # ``greffer_updater_image`` is a digest-pinned ref, and the updater only
    # recreates cosign-signed images at/above the ``min_supported`` floor,
    # fail-closed. So default-on means "auto-accept signed Greffon releases the
    # manager triggers", never "run arbitrary code". An operator who wants the
    # node to refuse remote updates sets GREFFER_REMOTE_UPDATE_ENABLED=false. The
    # value is advertised in the register payload so the manager knows whether to
    # offer the button. Carries the ``greffer_`` prefix to bind
    # GREFFER_REMOTE_UPDATE_ENABLED (the prefix pitfall noted above).
    greffer_remote_update_enabled: bool = True

    # Remote-update wiring (greffer self-update v2), only consulted when
    # ``greffer_remote_update_enabled`` is true. ``greffer_updater_image`` is the
    # DIGEST-PINNED signed updater image the controller spawns
    # (``greffon/greffer-updater@sha256:...``); pinning by digest is what stops a
    # moved-tag swap of the one container that recreates the greffer. Left empty
    # by default so an operator who flips the flag without wiring the image gets a
    # clear refusal from the route (not a silent ``:latest`` pull).
    # ``greffer_version_manifest_url`` is the signed version manifest the updater
    # fetches to compute the ``min_supported`` floor; the same default the v1 CLI
    # uses. Both carry the ``greffer_`` prefix to bind GREFFER_UPDATER_IMAGE /
    # GREFFER_VERSION_MANIFEST_URL (the prefix pitfall noted above).
    greffer_updater_image: str = ""
    greffer_version_manifest_url: str = "https://greffon.io/greffer-version.json"

    # Self-health watchdog (greffer-observability epic, Feature #3). The
    # watchdog evaluates /readyz's FATAL conditions and, when one is sustained
    # past the grace window, exits the uvicorn process so ``restart:
    # unless-stopped`` recovers it (plain ``docker compose`` does NOT restart a
    # container on an unhealthy healthcheck). On by default. Degraded states
    # (e.g. registration pending acceptance) are NEVER fatal, so a greffer
    # awaiting acceptance does not restart-loop. ``grace`` rides out transient
    # docker blips. Field names carry the ``greffer_`` prefix to bind the
    # documented GREFFER_WATCHDOG_* env vars (see prefix pitfall above).
    greffer_watchdog_enabled: bool = True
    greffer_watchdog_interval: int = 10
    greffer_watchdog_grace: int = 30
    # Upper bound on a single readiness probe so a HUNG (not just down) docker
    # daemon can't block the watchdog inside the ping forever — otherwise it
    # would never advance the grace clock or reach the restart for exactly the
    # docker failure it exists to heal. A probe that exceeds this is itself
    # treated as fatal. Keep < interval and < grace.
    greffer_watchdog_probe_timeout: int = 5

    # Compose log rotation for the greffon INSTANCE containers the greffer
    # renders (the real disk risk: an instance can log unbounded under the
    # docker json-file driver's default of no rotation). Injected per service
    # in ``create_compose`` unless the catalog author already set ``logging``.
    # The greffer's OWN services carry static logging in docker-compose.yml.
    greffer_instance_log_max_size: str = "10m"
    greffer_instance_log_max_file: int = 3

    @field_validator("greffer_instance_log_max_file", mode="before")
    @classmethod
    def _coerce_log_max_file(cls, v):
        # An operator typo (e.g. GREFFER_INSTANCE_LOG_MAX_FILE=2x) must NOT crash
        # the app at pydantic parse and take down ALL instance operations over an
        # optional logging knob; fall back to the default (codex P2 on
        # greffon/greffer#72). Mirrors _inject_instance_log_rotation's fallback.
        try:
            return int(v)
        except (TypeError, ValueError):
            return 3

    # Per-greffer concurrency cap on blocking metrics/disk collection
    # (resource-monitoring epic, Feature 2). The pull endpoints offload their
    # blocking Docker/FS work via ``anyio.to_thread.run_sync`` under a DEDICATED
    # CapacityLimiter of this size, distinct from the default AnyIO request
    # threadpool limiter (40) that serves start/stop. Kept well below 40 so a
    # saturating metrics fan-out can never consume the tokens start/stop needs:
    # the head-of-line block the epic AC guards against. Field name carries the
    # ``greffer_`` prefix to bind GREFFER_METRICS_CONCURRENCY (the prefix
    # pitfall documented on greffer_workers_enabled).
    greffer_metrics_concurrency: int = 8

    @field_validator("greffer_metrics_concurrency", mode="before")
    @classmethod
    def _coerce_metrics_concurrency(cls, v):
        # A bad optional knob must not crash startup and take down ALL instance
        # operations (the codex P2 lesson from Features #3/#4). Floor at 1, cap
        # at 32 to preserve start/stop headroom under the 40-token AnyIO
        # limiter; coerce anything malformed to the default.
        try:
            return min(32, max(1, int(v)))
        except (TypeError, ValueError):
            return 8

    # Log surfacing (resource-monitoring epic, Feature 2, logs slice). Default
    # ON as of the rollout: the security review cleared the streams (only the
    # owner's own container/deploy output is surfaced, and the docker-compose
    # env is scrubbed of greffer secrets so a hostile catalog can't exfiltrate
    # the token). When off, the logs endpoint 404s at the SOURCE. An operator
    # who does not want logs surfaced sets GREFFER_LOG_SURFACING_ENABLED=false.
    # Field name carries the ``greffer_`` prefix to bind
    # GREFFER_LOG_SURFACING_ENABLED (the prefix pitfall on greffer_workers_enabled).
    greffer_log_surfacing_enabled: bool = True

    logger_name: str = "greffer"

    # Structured logging (greffer-observability epic, Feature #4). JSON is the
    # default; ``text`` stays selectable as a one-release escape hatch. Field
    # names carry the ``greffer_`` prefix to bind GREFFER_LOG_FORMAT /
    # GREFFER_LOG_LEVEL (the prefix pitfall above). Both validators coerce a
    # malformed value to the default rather than crashing startup (the codex P2
    # lesson from Feature #3: a bad optional knob must not take the greffer down).
    greffer_log_format: Literal["json", "text"] = "json"
    greffer_log_level: str = "INFO"

    @field_validator("greffer_log_format", mode="before")
    @classmethod
    def _coerce_log_format(cls, v):
        return v if v in ("json", "text") else "json"

    @field_validator("greffer_log_level", mode="before")
    @classmethod
    def _coerce_log_level(cls, v):
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        return v.upper() if isinstance(v, str) and v.upper() in valid else "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
