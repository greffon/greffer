"""Deploy-time preflight (compose-containment Feature 6, the validator half;
the WIRE contract is owned by the custom-app foundation LLD §4.1):

    POST /api/controller/preflight/  {"compose_yaml": "..."}
      200 {"services": [...], "ports": [...], "config_candidates": [...],
           "image_digests": {...}, "warnings": [...]}
      422 {"errors": [{"service", "key", "message"}, ...]}

The preflight runs the SAME parse/validation path as a real start
(normalize_compose, strict set — preflight is FOR custom composes), so a
green preflight provably implies a start that parses. Read-only apart from
one deliberate node-side write: image digest resolution pull+inspects each
``image:`` reference (layer cache — bounded by disk admission at start;
a digest that cannot be resolved is an error naming the image, never a
deploys-unpinned fallback).
"""
from __future__ import annotations

import logging
import re

import yaml

from apps.utils.docker import compose as compose_mod
from apps.utils.greffon.compose_shape import (
    AliasForbiddenError, ComposeShapeError, normalize_compose,
    parse_compose_yaml,
)

logger = logging.getLogger("greffer")


class PreflightError(Exception):
    """Refusal with structured rows, surfacing verbatim from the shape
    pass (the LLD's 422 contract) or from digest resolution."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"preflight refused: {errors}")


# Both reference forms, like the integration strip's pattern: dot
# ({{ config.X }}) and bracket ({{ config['X'] }}) are semantically
# identical Jinja, and a bracket-only reference must still pre-tick its
# candidate (review finding: dot-only let it through as unreferenced --
# the silent-empty class the create-time check exists to prevent).
# config.NAME / config['NAME'] / config["NAME"] ANYWHERE inside an
# expression (codex review: matching only at the {{ opening token missed
# {{ 'p' ~ config.SECRET }} and conditional blocks). Groups 1/3 are the
# names; group 2 is the quote DELIMITER and must never become a
# candidate (also codex).
_CONFIG_REF = re.compile(
    r"config(?:\.([A-Za-z0-9_]+)|\[(['\"])([A-Za-z0-9_]+)\2)")


def _CONFIG_KEYS(groups):
    return {g for i, g in enumerate(groups) if g and i != 1}
def _strip_expressions(value: str) -> str:
    """Remove every template expression span, LINEARLY: a non-greedy
    dot-star regex backtracked quadratically on brace-padded values
    (256KB -> 210s, adversarial review), pinning a threadpool token.
    A single left-to-right scan has the same semantics for well-formed
    templates; unterminated {{ leaves the tail as literal text (the
    conservative direction for the warning)."""
    out = []
    i = 0
    n = len(value)
    while i < n:
        start = value.find("{{", i)
        if start == -1:
            out.append(value[i:])
            break
        out.append(value[i:start])
        end = value.find("}}", start + 2)
        if end == -1:
            out.append(value[start:])  # unterminated: keep as literal
            break
        i = end + 2
    return "".join(out)
# Mirrors _delete_unset_integration_env_keys' pattern: a word-bounded
# integration type name followed by `.attr` or `['key` — matched against
# the value with all {{ ... }} expressions REMOVED, so only LITERAL text
# (an innocent 'smtp.gmail.com' in a templated MAIL_URL) warns; a genuine
# {{ smtp.host }} reference lives inside the removed span and the strip
# deleting it is correct behaviour, not a warning.
_TYPE_LITERAL = {
    t: re.compile(r"\b" + re.escape(t) + r"(?:\.\w|\[)")
    for t in compose_mod.KNOWN_INTEGRATION_TYPES
}


def _env_items(service_def: dict):
    env = service_def.get("environment")
    if isinstance(env, dict):
        for k, v in env.items():
            yield str(k), v
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str) and "=" in item:
                name, value = item.split("=", 1)
                yield name.strip(), value


_LEAF_BUDGET = 20000  # visits per walk; a 1 MiB compose has << this many


def _string_leaves(value):
    """Alias-safe, cycle-safe, budget-bounded walk (codex review: YAML
    aliases let a sub-KiB compose expand to exponentially many visits —
    or recurse forever with a self-referencing alias — pinning a request
    thread outside every render guard). Shared containers are visited
    ONCE; the visit budget caps total work for pathological shapes."""
    seen = set()
    budget = [_LEAF_BUDGET]

    def walk(node):
        if budget[0] <= 0:
            raise PreflightError([{
                "service": None, "key": "compose_yaml",
                "message": "compose structure too large to analyze "
                           "(alias expansion or excessive nesting)",
            }])
        budget[0] -= 1
        if isinstance(node, (dict, list)):
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    yield from walk(value)


def _strip_warnings(services: dict) -> list[dict]:
    warnings = []
    for name, svc in services.items():
        for key, value in _env_items(svc):
            if not isinstance(value, str) or "{{" not in value:
                continue
            literal = _strip_expressions(value)
            for itype, pattern in _TYPE_LITERAL.items():
                if pattern.search(literal):
                    warnings.append({
                        "service": name,
                        "key": key,
                        "message": (
                            f"this value's literal text mentions "
                            f"'{itype}' outside its template expression; "
                            f"the unset-integration env strip deletes the "
                            f"whole key when no {itype} integration is "
                            f"selected"),
                    })
    return warnings


def _resolve_image_digests(services: dict) -> dict:
    """Pull+inspect every image: reference and return {service: digest}.
    A reference that cannot be resolved is an error naming the image —
    the artifact pin refuses rather than falling back to unpinned bytes.
    """
    digests = {}
    for name, svc in services.items():
        image = svc.get("image")
        if not isinstance(image, str) or not image:
            # The allowlist PERMITS the key but never REQUIRES it, and
            # strict mode refuses `build` — so an imageless service
            # cannot deploy at all. Say so instead of skipping silently
            # (codex review).
            raise PreflightError([{
                "service": name,
                "key": "image",
                "message": "service has no image reference and cannot "
                           "deploy (build is not permitted)",
            }])
        try:
            compose_mod.client.images.pull(image)
            info = compose_mod.client.images.get(image)
            repo_digests = [
                d for d in (info.attrs.get("RepoDigests") or [])
                if "@" in d
            ]
            if not repo_digests:
                raise RuntimeError("image has no repo digest")
            digests[name] = repo_digests[0]
        except Exception as exc:  # noqa: BLE001 — surfaced as a named row
            logger.warning("preflight digest failed for %s: %s", image, exc)
            raise PreflightError([{
                "service": name,
                "key": "image",
                "message": f"could not resolve {image!r} to a digest "
                           f"({type(exc).__name__}); an unpinned reference "
                           f"is not deployable",
            }]) from exc
    return digests


def run_preflight(compose_yaml: str, extra_keys=None) -> dict:
    """Validate a custom compose for deployability. Raises PreflightError
    with 422 rows; returns the LLD §4.1 payload on success."""
    # parse_compose_yaml (compose_shape) refuses alias REFERENCES at
    # load: the preflight's own walks are alias-safe, but a green
    # preflight's bytes go on to /start/, whose render-accounting walk
    # runs in the parent process outside the child render timeout — a
    # sub-KiB anchor DAG there pins the single-worker controller
    # (adversarial review on the transport PR). The allowlist permits no
    # x- extension fields, so an anchor pattern was never deployable;
    # refusing it here names it for the user at create time.
    try:
        compose = parse_compose_yaml(compose_yaml)
    except AliasForbiddenError as exc:
        raise PreflightError([{
            "service": None, "key": "compose_yaml", "message": str(exc),
        }]) from exc
    except (yaml.YAMLError, RecursionError) as exc:
        raise PreflightError([{
            "service": None, "key": "compose_yaml",
            "message": f"not valid YAML "
                       f"({getattr(exc, 'problem', None) or exc})",
        }]) from exc

    try:
        normalize_compose(compose, strict=True, extra_keys=extra_keys)
    except ComposeShapeError as exc:
        raise PreflightError([{
            "service": exc.service, "key": exc.key, "message": exc.message,
        }]) from exc

    services = compose["services"]

    ports = []
    for name, svc in services.items():
        for port in svc.get("ports") or []:
            ports.append({
                "port_name": f"{name}_{port['target']}",
                "service": name,
                "container_port": str(port["target"]),
                "protocol": port.get("protocol") or "tcp",
            })

    referenced = set()
    for leaf in _string_leaves(compose):
        for groups in _CONFIG_REF.findall(leaf):
            referenced.update(_CONFIG_KEYS(groups))
    candidates = {}
    for name, svc in services.items():
        for key, _value in _env_items(svc):
            entry = candidates.setdefault(
                (name, key), {"service": name, "key": key,
                              "referenced": False})
            if key in referenced:
                entry["referenced"] = True
    known = {k for (_s, k) in candidates}
    for key_name in referenced:
        if key_name not in known:
            known.add(key_name)
            candidates[(None, key_name)] = {
                "service": None, "key": key_name, "referenced": True}

    return {
        "services": sorted(services),
        "ports": ports,
        "config_candidates": sorted(candidates.values(),
                                    key=lambda c: (c["service"] or "",
                                                   c["key"])),
        "image_digests": _resolve_image_digests(services),
        "warnings": _strip_warnings(services),
    }
