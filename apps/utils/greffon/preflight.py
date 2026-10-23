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
import shutil

import docker.errors
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


def _disk_free_bytes(settings) -> int | None:
    """statvfs on greffon_path — the heartbeat's existing disk-free
    signal (a real filesystem call; docker df() walks every volume on
    the host and is the most expensive call in the design). None means
    "could not read" and the headroom check is skipped (fail-open,
    logged): a transient statvfs failure must not turn into a wrong
    create refusal."""
    try:
        return shutil.disk_usage(str(settings.greffon_path)).free
    except OSError as exc:
        logger.warning(
            "disk admission could not read free space at %s: %s",
            settings.greffon_path, exc)
        return None


def _remove_newly_pulled(image: str) -> None:
    """HLD §9 cleanup: a refused pull must not leave the layers it
    fetched. Only ever called when the image did NOT exist before this
    preflight pulled it — removing a pre-existing image would break the
    neighbour already using it. Contained by design: a cleanup failure
    is logged, never raised, so it cannot mask the refusal that
    triggered it."""
    try:
        compose_mod.client.images.remove(image)
    except Exception as exc:  # noqa: BLE001 — contained, see docstring
        logger.warning(
            "disk admission cleanup could not remove refused image "
            "%s: %s", image, exc)


def _admit_image(name, image, info, post_id, introduced,
                  settings) -> None:
    """Disk admission (HLD §9): judge the image NOW, after the pull —
    the bytes are on disk, which is exactly what the caps judge — and
    refuse with a named row (the manager relays it at create time). A
    refused image the pull just introduced is removed (cleanup pass).

    Grain notes (review-confirmed): the cleanup is per-image — a
    multi-service create refused on its SECOND image keeps the first
    image as warm layer cache, never half-deployed state. Size is the
    VIRTUAL size (all layers incl. shared bases), so a fully-cached
    image can still trip the margin on a nearly-full disk — a
    conservative refusal that recovers no space, on purpose: a new app
    should not deploy onto a disk below the operator's margin."""
    size = int((info.attrs or {}).get("Size") or 0)
    if settings.greffer_image_max_bytes and size > settings.greffer_image_max_bytes:
        if introduced:
            # By ID, not name: on a moved tag the name still belongs to
            # the OLD image too, and removing by name could untag
            # something a neighbour uses (codex).
            _remove_newly_pulled(post_id or image)
        gib = 1024 ** 3
        raise PreflightError([{
            "service": name,
            "key": "image",
            "message": f"image {image!r} is {size / gib:.1f} GiB; the "
                       f"operator's per-image cap is "
                       f"{settings.greffer_image_max_bytes / gib:.1f} GiB "
                       f"(GREFFER_IMAGE_MAX_BYTES)",
        }])
    if settings.greffer_image_min_free_bytes:
        free = _disk_free_bytes(settings)
        if free is not None and (
                size + settings.greffer_image_min_free_bytes > free):
            if introduced:
                _remove_newly_pulled(post_id or image)
            mib = 1024 ** 2
            raise PreflightError([{
                "service": name,
                "key": "image",
                "message": f"image {image!r} is {size / mib:.0f} MiB and "
                           f"only {free / mib:.0f} MiB is free; the "
                           f"operator keeps "
                           f"{settings.greffer_image_min_free_bytes / mib:.0f}"
                           f" MiB free (GREFFER_IMAGE_MIN_FREE_BYTES)",
            }])


def _resolve_image_digests(services: dict, settings=None) -> dict:
    """Pull+inspect every image: reference and return {service: digest}.
    A reference that cannot be resolved is an error naming the image —
    the artifact pin refuses rather than falling back to unpinned bytes.
    With `settings`, each pull passes disk admission (HLD §9): the
    count cap first, then per-image size and free-space checks.
    """
    digests = {}
    count = 0
    count_cap_active = bool(settings is not None
                            and settings.greffer_image_count_max)
    if count_cap_active:
        try:
            # The low-level API, NOT images.list(): the model method
            # inspects every image (an N+1 storm — 301 daemon calls on a
            # 300-image node) on the same threadpool token the pulls
            # hold. One /images/json response carries the whole count
            # (adversarial review).
            count = len(compose_mod.client.api.images())
        except Exception as exc:  # noqa: BLE001 — fail-open, logged
            count_cap_active = False
            logger.warning(
                "disk admission could not count images: %s", exc)
        else:
            if count >= settings.greffer_image_count_max:
                raise PreflightError([{
                    "service": None,
                    "key": "image",
                    "message": f"the node already holds {count} images "
                               f"(cap {settings.greffer_image_count_max}; "
                               f"GREFFER_IMAGE_COUNT_MAX)",
                }])
    # Images THIS preflight introduces consume count-cap slots as the
    # loop goes: with one cached image, a cap of 2 and two new images,
    # the snapshot check alone would let one request land 3 (codex).
    slots_used = 0
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
        # Was this IMAGE already on the node before this preflight?
        # Probed by IDENTITY, not tag presence: a mutable tag can be
        # cached at digest A while the registry now serves B — the pull
        # then introduces a NEW image under the same name (codex). When
        # the probe is unreadable, cleanup is skipped entirely (doubt
        # means "not ours to delete").
        pre_id = None
        pre_unclear = False
        try:
            pre_id = compose_mod.client.images.get(image).attrs.get("Id")
        except docker.errors.ImageNotFound:
            pre_id = None
        except Exception as exc:  # noqa: BLE001 — unreadable -> skip cleanup
            pre_unclear = True
            logger.warning(
                "preflight could not check image pre-existence of %s: %s",
                image, exc)
        # The pre-pull headroom precondition (HLD §9 "before any pull"):
        # with the disk already at or below the operator's margin there
        # is no honest size to declare, and the pull itself could run
        # the shared disk to ENOSPC — bypassing admission AND cleanup
        # (codex P1). The exact size-vs-margin judgment still runs
        # post-pull.
        if settings is not None and settings.greffer_image_min_free_bytes:
            free_pre = _disk_free_bytes(settings)
            if free_pre is not None and (
                    free_pre <= settings.greffer_image_min_free_bytes):
                raise PreflightError([{
                    "service": name,
                    "key": "image",
                    "message": f"the node's free space "
                               f"({free_pre / 1024 ** 2:.0f} MiB) is at or "
                               f"below the operator's keep-free margin "
                               f"({settings.greffer_image_min_free_bytes / 1024 ** 2:.0f}"
                               f" MiB); refusing before pulling {image!r} "
                               f"(GREFFER_IMAGE_MIN_FREE_BYTES)",
                }])
        # A count-cap slot for every image this pull will introduce
        # (tag absent and probe readable): the snapshot check above
        # cannot see this request's own additions.
        if count_cap_active and not pre_unclear and pre_id is None:
            if count + slots_used >= settings.greffer_image_count_max:
                raise PreflightError([{
                    "service": name,
                    "key": "image",
                    "message": f"pulling {image!r} would take the node to "
                               f"{count + slots_used + 1} images (cap "
                               f"{settings.greffer_image_count_max}; "
                               f"GREFFER_IMAGE_COUNT_MAX)",
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
        except Exception as exc:  # noqa: BLE001 — surfaced as a named row
            logger.warning("preflight digest failed for %s: %s", image, exc)
            raise PreflightError([{
                "service": name,
                "key": "image",
                "message": f"could not resolve {image!r} to a digest "
                           f"({type(exc).__name__}); an unpinned reference "
                           f"is not deployable",
            }]) from exc
        digests[name] = repo_digests[0]
        if settings is not None:
            post_id = info.attrs.get("Id")
            # A daemon that omitted Id from inspect would make the
            # fallback remove-by-name reachable with a moved tag —
            # unreachable on real engines, but one line to make it
            # impossible (closure-verified hardening).
            introduced = (bool(post_id) and not pre_unclear
                          and pre_id != post_id)
            if introduced:
                slots_used += 1
            _admit_image(name, image, info, post_id, introduced, settings)
    return digests


def run_preflight(compose_yaml: str, extra_keys=None, settings=None) -> dict:
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
        "image_digests": _resolve_image_digests(services,
                                                settings=settings),
        "warnings": _strip_warnings(services),
    }
