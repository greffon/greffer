from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# Defense-in-depth: restrict `id` to safe filename-like characters. The id
# is path-joined with $GREFFON_PATH inside the shared compose utilities, so
# a compromised/buggy manager sending `"../.."` would escape the data root.
# The trust boundary assumes the manager is trusted; this constraint costs
# nothing and covers all legitimate payloads (UUIDs + existing
# `test-instance-*` names in tests).
_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class Certificate(BaseModel):
    # Django serializer misspells this as "Cerificate"; internal-only,
    # never serialized by name. Pydantic side uses the correct spelling.
    certificate: str
    private_key: str


class GreffonField(BaseModel):
    value: Any
    destinations: Any


class GreffonStartRequest(BaseModel):
    # `id` is a free-form str in DRF (not UUID), matching what the manager
    # sends today (e.g. "test-instance-123"). Pattern kept permissive to
    # accept UUIDs and existing ID formats; rejects path-traversal.
    id: str = Field(pattern=_ID_PATTERN, min_length=1, max_length=128)
    # `min_length=1` matches DRF's `CharField` default (rejects blank).
    # Otherwise an empty URL reaches `requests.get('')` and 500s instead
    # of returning a 400 validation error.
    repository_url: str = Field(min_length=1)
    cert: Certificate
    # Optional in the DRF sense (may be omitted from the payload), but
    # defaulted to an empty container here so the dumped dict always has
    # the key present. `create_greffon_info` in
    # apps/utils/greffon/repository.py uses strict `greffon['configurations']`
    # access, not `.get(...)`, so omitting the key → KeyError → 500.
    # Explicit `null` is rejected on type grounds (list, not list | None).
    configurations: list[GreffonField] = Field(default_factory=list)
    ports: dict[str, Any] = Field(default_factory=dict)
    # Feature #4 (integrations): per-type config blobs the manager
    # resolves from the user's selected Integration rows. Each top-level
    # key is an integration type ("smtp", later "telegram", "slack");
    # the value is the type-specific config (e.g. for smtp: host, port,
    # username, password, from_address, tls_mode).
    #
    # Default empty dict is the wire-compat story: an old manager that
    # doesn't send this field at all just looks like "user picked no
    # integrations" — the greffer renders compose with the catalog-
    # declared SMTP env keys stripped (see compose.py's
    # _delete_unset_integration_env_keys). Symmetric on the other
    # direction: a new manager → old greffer trip ignores this field
    # via `extra=ignore` rather than 422-ing.
    integrations: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}

    # v3 manager-pushed rathole client config. When present (tunnel-mode
    # greffer with a v3 manager), the controller atomically writes it to
    # the shared volume rathole-client file-watches. Absent for proxy-
    # mode greffers and for the transitional v2-manager-+-v3-greffer
    # combination during rollout step 1 (in that combination, tunnel
    # config still flows via the v2 polling path). See tunnel-support
    # epic v3 §4 "Pull-based sidecar replaced by manager-pushed config"
    # and the rollout-ordering section.
    tunnel_client_toml: str | None = None


class GreffonStopRequest(BaseModel):
    id: str = Field(pattern=_ID_PATTERN, min_length=1, max_length=128)
    # Same shape as start: optional client.toml pushed by manager. Stop
    # is the only place where a stale client.toml on the greffer is
    # harmless until next start (the dropped server.toml service severs
    # traffic regardless), but a fresh stop push lets rathole-client
    # close its idle forwarding pair right away.
    tunnel_client_toml: str | None = None


# config_write_status — surfaced in start/stop responses so the manager
# can report a greffer-side write failure to the API caller. ``ok``
# means the file was written atomically (or the field was absent and
# nothing needed writing); ``failed`` means an OSError on write — the
# instance start/stop itself still succeeded but the tunnel config is
# now stale on disk and the next start/stop will re-push.
ConfigWriteStatus = Literal["ok", "failed"]


class GreffonStartResponse(BaseModel):
    ports: list[Any]
    config_write_status: ConfigWriteStatus = "ok"


class GreffonStopResponse(BaseModel):
    config_write_status: ConfigWriteStatus = "ok"


class GreffonStatusResponse(BaseModel):
    status: str
    containers: list[dict[str, Any]]


class TunnelConfigPushRequest(BaseModel):
    """v3 manager-pushed rathole client.toml — second-phase push for
    start/stop flows.

    Manager makes the controller-start/stop call FIRST, gets the
    greffer's port_host allocation in the response, then renders
    client.toml against the post-allocation state and pushes it via
    this endpoint. The split exists because the rendered file's
    ``local_addr`` lines depend on port_host, which manager doesn't
    know until the greffer responds. See manager-side PR E and epic
    v3 § "start_stop_greffon — start path".

    On accept_register, the initial client.toml ships in the cert
    response body instead (the greffer is the caller of that hop;
    response-body delivery is the natural shape there). See
    ``app/workers/register.py``.
    """
    client_toml: str = Field(min_length=1)


class TunnelConfigPushResponse(BaseModel):
    """Mirrors ``GreffonStartResponse.config_write_status`` so the
    manager's surfacing logic can use the same field name across
    every push call site."""
    config_write_status: ConfigWriteStatus = "ok"
