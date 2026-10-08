"""Compose-shape normalization (compose-containment Feature 2).

A single pass in front of ``create_greffon_info`` that turns every legal
compose spelling of ports / volumes / networks into one dict-or-list shape,
and refuses what the pipeline cannot honour with a structured error naming
the service and the key — never an uncaught ``AttributeError``/
``ValueError``/``KeyError`` surfaced as a bare 500 (the seven-construct
failure table in the epic's Context).

The contract with ``create_greffon_info`` after this pass:

- ``services`` is a dict of dicts (missing -> error);
- every service's ``ports`` is a list of ``{"target": str, "protocol": str|None}``;
- every service's ``volumes`` is a list of ``{"source": str, "target": str}``;
- top-level ``networks`` (if present) is a dict whose keys the services may
  reference; every service's ``networks`` is a list of declared names;
- ``ports``/``volumes`` in any other shape (a mapping, a bare string) is
  refused — compose itself only accepts lists here, and the legacy dead
  ``else`` branches that pretended to handle them referenced unbound locals.

Extraction equivalence (the catalog regression bar): for the short forms the
catalog uses, the values ``create_greffon_info`` derives — ``port_name``,
protocol, volume source/target — are identical before and after; the shapes
that previously crashed now either normalize to those same values or refuse
with a diagnosis. The rebuild in ``create_compose_template_from_greffon``
erases and re-derives service ``ports``/``volumes``/``networks`` anyway, so
the rendered compose cannot change for entries that parsed before; a
preserved mount mode (``:ro``) is re-rendered onto the rebuilt mapping, so
a read-only declaration stays read-only.
"""

from __future__ import annotations

# Access modes docker documents for the third short-syntax mount field (the
# long-syntax ``mode`` key honours ro/rw). A preserved mode is re-rendered
# onto the rebuilt mapping (Feature 2: a read-only declaration stays
# read-only); an unknown token is far more likely a typo ("vol:/p:banna")
# than a mode we should silently drop, so only the documented set is accepted.
_MOUNT_MODES = frozenset(
    {"ro", "rw", "z", "Z", "cached", "delegated", "consistent", "default"}
)

_PROTOCOLS = frozenset({"tcp", "udp"})

# compose's implicit default network: a service may reference it without a
# top-level declaration.
_IMPLICIT_NETWORKS = frozenset({"default"})


class ComposeShapeError(Exception):
    """A compose the pipeline cannot honour, with the offending path.

    Carries ``service``/``key``/``value`` as attributes for programmatic use
    (the future preflight surfaces them verbatim) and renders as
    ``services.<service>.<key>: <message>`` so a 422 detail reads like the
    YAML path the user must fix.
    """

    def __init__(self, service: str | None, key: str, message: str, value=None):
        self.service = service
        self.key = key
        self.value = value
        self.message = message
        location = f"services.{service}.{key}" if service else key
        suffix = f" (got {value!r})" if value is not None else ""
        super().__init__(f"{location}: {message}{suffix}")


def _bad(service, key, message, value=None):
    return ComposeShapeError(service, key, message, value)


def _normalize_port(entry, service: str) -> dict:
    """One ``ports`` entry (every legal spelling) -> {"target", "protocol"}."""
    if isinstance(entry, int) and not isinstance(entry, bool):
        target, protocol = str(entry), None
    elif isinstance(entry, str):
        # "8080" | "80:80" | "127.0.0.1:8080:80" | "8080/udp" | "80:80/udp"
        # — the container port is the LAST colon-separated segment, matching
        # the legacy `port.split(':')[-1]` extraction.
        raw = entry
        protocol = None
        if "/" in raw:
            raw, proto = raw.rsplit("/", 1)
            protocol = proto.lower()
        target = raw.rsplit(":", 1)[-1]
    elif isinstance(entry, dict):
        # Long syntax: {target, published?, protocol?, mode?}. ``published``
        # (the host port) carries no information downstream — the greffer
        # rebuilds every mapping from its own allocation — so it is read only
        # to be named in errors.
        target = entry.get("target")
        protocol = entry.get("protocol")
        if "target" not in entry:
            raise _bad(service, "ports", "long-syntax port requires 'target'", entry)
    else:
        raise _bad(
            service,
            "ports",
            f"unsupported ports entry type ({type(entry).__name__})",
            entry,
        )

    if isinstance(target, int) and not isinstance(target, bool):
        # Long syntax allows an integer target ("target": 80).
        target = str(target)
    if (
        not isinstance(target, str)
        or not target.isdigit()
        or not (0 < int(target) < 65536)
    ):
        if isinstance(target, str) and "-" in target:
            hint = "port ranges are not supported; declare each port as its own entry"
        elif isinstance(entry, dict):
            hint = "long-syntax 'target' must be the container port"
        else:
            hint = "container port must be an integer"
        raise _bad(service, "ports", hint, entry)
    if protocol is not None and protocol not in _PROTOCOLS:
        raise _bad(
            service, "ports", f"protocol must be one of {sorted(_PROTOCOLS)}", entry
        )
    return {"target": target, "protocol": protocol}


def _normalize_volume(entry, service: str) -> dict:
    """One ``volumes`` entry -> {"source", "target"}. Bind mounts and
    anonymous volumes are refused: the greffer runs docker-compose inside its
    own container against the host daemon, so a host path resolves against
    the greffer's filesystem, not the operator's — structurally unsupported,
    not merely unsafe."""
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) == 2:
            source, target, mode = parts[0], parts[1], None
        elif len(parts) == 3 and parts[2] in _MOUNT_MODES:
            source, target, mode = parts[0], parts[1], parts[2]
        elif len(parts) == 3:
            raise _bad(
                service,
                "volumes",
                f"unknown mount mode (known: {sorted(_MOUNT_MODES)})",
                entry,
            )
        elif len(parts) == 1:
            raise _bad(
                service,
                "volumes",
                "anonymous volumes are not supported; declare a named "
                "volume in the top-level volumes block",
                entry,
            )
        else:
            raise _bad(service, "volumes", "expected SOURCE:TARGET[:MODE]", entry)
    elif isinstance(entry, dict):
        vtype = entry.get("type", "volume")
        source = entry.get("source")
        target = entry.get("target")
        # Long syntax expresses access with `read_only` (a bool), NOT the
        # short syntax's `:ro` mode token — reading `mode` alone rendered a
        # declared read-only mount writable (codex review).
        if entry.get("read_only") is True:
            mode = "ro"
        else:
            mode = entry.get("mode") if entry.get("mode") in ("ro", "rw") else None
        # Nested volume options cannot be expressed in the rebuilt short
        # syntax: subpath mounts a SUBDIRECTORY (dropping it mounts the whole
        # volume), nocopy changes population semantics. Accepting the mount
        # while dropping the option deploys a DIFFERENT mount — refuse.
        nested = entry.get("volume")
        if isinstance(nested, dict) and nested:
            raise _bad(
                service,
                "volumes",
                "long-syntax volume options (subpath/nocopy) are not "
                "supported by the rebuilt mount; declare the subdirectory "
                "as its own top-level volume instead",
                entry,
            )
        if vtype != "volume":
            raise _bad(
                service,
                "volumes",
                f"volume type '{vtype}' is not supported; use a named "
                "volume (type: volume)",
                entry,
            )
        if "target" not in entry:
            raise _bad(
                service, "volumes", "long-syntax volume requires 'target'", entry
            )
        if not source:
            raise _bad(
                service,
                "volumes",
                "anonymous volumes are not supported; declare a named "
                "volume in the top-level volumes block",
                entry,
            )
    else:
        raise _bad(
            service,
            "volumes",
            f"unsupported volumes entry type ({type(entry).__name__})",
            entry,
        )

    if not source or not target:
        raise _bad(service, "volumes", "source and target must be non-empty", entry)
    if source.startswith(("/", ".", "~")):
        raise _bad(
            service,
            "volumes",
            "bind mounts are structurally unsupported on this platform; "
            "use a named volume declared in the top-level volumes block",
            entry,
        )
    if not target.startswith("/"):
        raise _bad(
            service, "volumes", "container target must be an absolute path", entry
        )
    return {"source": source, "target": target, "mode": mode}


def _normalize_service_networks(entry, service: str) -> list[str]:
    """Service ``networks`` (list of names, or mapping name -> {aliases...})
    -> list of names."""
    if isinstance(entry, str):
        names = [entry]
    elif isinstance(entry, (list, dict)) and all(isinstance(n, str) for n in entry):
        # list of names, or mapping name -> {aliases...} (keys are the names)
        names = list(entry)
    else:
        raise _bad(
            service,
            "networks",
            "expected a list of network names or a mapping of name to aliases",
            entry,
        )
    # An EMPTY list is legal compose (opt out of the default network); the
    # pipeline still places the service on greffon_internal_network, so it
    # is a no-op here, not a refusal.
    return names


# --- Key allowlist (compose-containment Feature 3) -------------------------
#
# A positive allowlist, not a denylist: a denylist is defeated by whatever it
# failed to enumerate (devices, volumes_from, build, ipc, userns_mode, and
# whatever a future compose spec adds). Trust-scoped, because a flat list
# breaks the catalog: wireguard/1.0 legitimately uses cap_add, cap_drop,
# devices and sysctls (PR-reviewed use), and refusing them takes a shipped
# app offline on its next deploy.
#
#   catalog: PR-reviewed input — the broad set including the privileged keys
#   strict:  unreviewed input (a custom compose) — no privileged keys
#
# The greffer-managed keys (ports/volumes/networks) are consumed and rebuilt
# by the pipeline, not passed through. ``env_file`` is refused in BOTH sets:
# compose resolves it client-side relative to the project directory, so
# ``env_file: ../<other-instance>/realm.json`` reads another instance's baked
# secrets and injects them as environment — the same filesystem reach the
# bind-mount refusal closes, by another door.

_TOP_KEYS = frozenset({"version", "services", "volumes", "networks"})

_MANAGED_SERVICE_KEYS = frozenset({"ports", "volumes", "networks"})

_PRIVILEGED_SERVICE_KEYS = frozenset(
    {"cap_add", "cap_drop", "devices", "sysctls"}
)

_BASE_SERVICE_KEYS = frozenset(
    {
        "image", "environment", "command", "entrypoint", "depends_on",
        "healthcheck", "labels", "restart", "user", "working_dir", "tmpfs",
        "stop_grace_period", "expose", "init", "platform", "shm_size",
        "ulimits",
        # logging: _inject_instance_log_rotation explicitly preserves an
        # author-declared block instead of injecting the rotation default;
        # refusing the key would make that documented contract unreachable
        # (codex review on the pre-fix revision caught the omission).
        "logging",
    }
)

# env_file is deliberately absent from both sets (see block comment), and
# is NEVER grantable via the operator override: it re-opens the
# cross-instance baked-secret read, a hole that harms tenants rather than
# the machine owner who would be granting it.
_NEVER_GRANTABLE = frozenset({"env_file"})

_SERVICE_KEYS_CATALOG = _BASE_SERVICE_KEYS | _PRIVILEGED_SERVICE_KEYS
_SERVICE_KEYS_STRICT = _BASE_SERVICE_KEYS


def _parse_extra_keys(raw) -> frozenset:
    """GREFFER_COMPOSE_EXTRA_ALLOWED_KEYS: comma-separated service keys an
    operator grants on their own node (dedicated greffers make this the
    machine owner's call). Tolerant of whitespace; empty means none."""
    if not raw:
        return frozenset()
    return frozenset(
        k.strip() for k in str(raw).split(",") if k.strip()
    )


def _validate_keys(compose, *, strict: bool, extra_keys) -> None:
    """Refuse every key not on the allowed list, naming the service and key.
    ``strict`` selects the custom (unreviewed) set; ``extra_keys`` is the
    operator's per-node grant, added to whichever set is in force."""
    granted = _parse_extra_keys(extra_keys) - _NEVER_GRANTABLE
    top_allowed = _TOP_KEYS
    service_allowed = (_SERVICE_KEYS_STRICT if strict else _SERVICE_KEYS_CATALOG) | granted
    for top_key in compose:
        if top_key not in top_allowed:
            raise _bad(
                None,
                top_key,
                "top-level key not permitted by the compose allowlist"
                + (" (catalog set)" if not strict else ""),
                top_key,
            )
    # A missing/!dict services block is the shape pass's own named refusal
    # below; iterating it here would turn that into a KeyError.
    services = compose.get("services")
    if not isinstance(services, dict):
        return
    for name, service_def in services.items():
        if not isinstance(service_def, dict):
            # The shape pass below refuses non-mapping service definitions
            # with a named error; iterating one here would turn that into
            # an uncaught TypeError (the bare-500 class this epic kills).
            continue
        for key in service_def:
            if key in service_allowed or key in _MANAGED_SERVICE_KEYS:
                continue
            raise _bad(
                name,
                key,
                "key not permitted"
                + (
                    " (custom composes use the strict set; ask the operator"
                    " to grant it via GREFFER_COMPOSE_EXTRA_ALLOWED_KEYS)"
                    if strict
                    else " (catalog set)"
                ),
                key,
            )


def normalize_compose(compose, *, strict: bool = False, extra_keys=None) -> dict:
    """Validate + normalize a parsed compose in place; return it.

    ``strict`` selects the custom (unreviewed) key set; ``extra_keys`` is the
    operator's per-node grant (see ``_validate_keys``). Raises
    ``ComposeShapeError`` on the first problem, naming the service and key.
    Idempotent: normalized shapes pass through unchanged.
    """
    if not isinstance(compose, dict):
        raise _bad(None, "compose", "top level must be a mapping", compose)
    _validate_keys(compose, strict=strict, extra_keys=extra_keys)
    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        raise _bad(
            None,
            "services",
            "compose requires a non-empty 'services' mapping",
            services if services is not None else "<missing>",
        )
    for service in services:
        if not isinstance(service, str) or not service:
            raise _bad(
                None, "services", "service names must be non-empty strings", service
            )
    top_networks = compose.get("networks") or {}
    if isinstance(top_networks, list):
        declared = {n for n in top_networks if isinstance(n, str)}
    elif isinstance(top_networks, dict):
        declared = set(top_networks)
    else:
        raise _bad(
            None,
            "networks",
            "top-level networks must be a mapping or a list",
            top_networks,
        )

    for name, service_def in services.items():
        if not isinstance(service_def, dict):
            raise _bad(
                name, "<service>", "a service definition must be a mapping", service_def
            )
        for key, normalizer in (
            ("ports", _normalize_port),
            ("volumes", _normalize_volume),
        ):
            entries = service_def.get(key)
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise _bad(
                    name,
                    key,
                    f"'{key}' must be a list; a mapping or bare value is "
                    "not valid compose",
                    entries,
                )
            service_def[key] = [normalizer(e, name) for e in entries]
        net_entry = service_def.get("networks")
        if net_entry is not None:
            names = _normalize_service_networks(net_entry, name)
            undeclared = [
                n for n in names if n not in declared and n not in _IMPLICIT_NETWORKS
            ]
            if undeclared:
                raise _bad(
                    name,
                    "networks",
                    f"network(s) not declared in the top-level networks "
                    f"block: {', '.join(sorted(set(undeclared)))}",
                    net_entry,
                )
            service_def["networks"] = names
    return compose
