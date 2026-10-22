"""The render budget (compose-containment Feature 1b — the second half of
Feature 1, which the sandbox deliberately deferred).

The sandbox (greffer#136) stops code execution and mutation; it does NOT
bound resource use. ``*``/``**`` are not intercepted, so a sandbox-legal
``{{ "A" * (10 ** 9) }}`` allocates ~1 GB — and Jinja CONSTANT-FOLDS it at
COMPILE time, before any output exists, so an output cap alone cannot catch
it. This module bounds the whole render, compile included, with three caps
plus isolation:

- input:  the template SOURCE (bytes) is capped before anything runs;
- output: the rendered string is accumulated via ``generate()`` and aborted
          the moment it exceeds its cap (a blow-up must pass through here);
- memory: the render runs in a dedicated ONE-WORKER child process whose
          ``RLIMIT_AS`` is capped, so a compile-time fold or any other
          allocation blow-up kills the CHILD (a MemoryError we convert to a
          clean refusal), never the ``--workers 1`` controller;
- time:   the parent waits with a timeout; on expiry the pool is dropped and
          rebuilt (a wedged worker must not poison the next render).

Isolation is the load-bearing half: caps are checked in the child, where a
violation is an exception, not an OOM of the process that holds the manager
token.

The caller-facing contract: ``render_bounded()`` returns the rendered string
or raises ``RenderBudgetExceeded`` — a ``ValueError`` SUBCLASS, so the
existing ``except`` tuples at both render call sites (baked files catch
ValueError; the body catches Exception) already map it to their
``ConfigRenderError`` 422s without new wiring.

Env knobs (read per call; renders are rare): ``GREFFER_RENDER_MAX_INPUT``
(default ``1048576``), ``GREFFER_RENDER_MAX_OUTPUT`` (default ``4194304``),
``GREFFER_RENDER_MEM_BYTES`` (default ``268435456``; also pins the child at
pool build — a change rebuilds the pool), ``GREFFER_RENDER_TIMEOUT_SECONDS``
(default ``5``). ``0`` disables that dimension — the operator's call, but
note BOTH foot-guns: without the memory guard a compile-time fold can
kill the controller's sibling work, and without the timeout a wedged
render holds an anyio threadpool token (shared with start/stop)
indefinitely.
"""
from __future__ import annotations

import concurrent.futures
import concurrent.futures.process
import logging
import math
import os
import sys
import threading

from jinja2 import StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment
logger = logging.getLogger("greffer")

_DEFAULT_INPUT = 1048576        # 1 MiB — compose bodies are tens of KB
_DEFAULT_OUTPUT = 4194304       # 4 MiB
_DEFAULT_MEM = 268435456        # 256 MiB
_DEFAULT_TIMEOUT = 5.0


class RenderBudgetExceeded(ValueError):
    """The render exceeded a cap. Message is a short machine code plus the
    cap — never rendered content (which may be the very thing that blew the
    budget). ValueError so the existing ConfigRenderError mappings catch it.
    """

    def __init__(self, code: str, cap):
        self.code = code
        self.cap = cap
        super().__init__(f"render_{code} (cap: {cap})")

    def __reduce__(self):
        # Exceptions raised in the child are PICKLED back to the parent;
        # the default pickling replays args (the formatted message), which
        # __init__ cannot accept. Round-trip through the constructor args.
        return (self.__class__, (self.code, self.cap))


def _knob(name: str, default: int, as_float: bool = False):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default) if as_float else default
    try:
        value = float(raw) if as_float else int(raw)
        if value < 0 or (as_float and not math.isfinite(value)):
            # inf makes Future.result raise OverflowError and nan times
            # out immediately — either way a typo'd knob breaks every
            # render instead of falling back (codex review).
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        return float(default) if as_float else default
    return value


# Strict Jinja environment for rendering baked `file`/`json` destination
# contents (feature: baked-config-files). Unlike the lenient ``Template``
# used for the compose body, a missing/typo'd variable here MUST raise
# rather than render to empty string: a baked Keycloak realm with an empty
# ``{{ config.OIDC_RP_CLIENT_SECRET }}`` is a silent security failure, so we
# fail the deploy loudly instead. ``autoescape=False`` because these are
# config files (JSON/conf), not HTML, and we must not HTML-escape values.
#
# SANDBOXED: the template string is catalog-author-controlled, and the catalog
# is community-extensible, so we must assume it can be hostile. A plain
# Environment would allow server-side template injection -> RCE on this worker
# (which holds the instance's minted secrets, the greffer's manager token, and
# Docker socket access), e.g.
# ``{{ cycler.__init__.__globals__.os.popen('id').read() }}``. SandboxedEnvironment
# blocks attribute traversal to unsafe objects while still rendering the
# legitimate ``{{ config.X }}`` / ``{{ instance_url }}`` references. A
# SecurityError surfaces as a clean ConfigRenderError (-> 422), like any other
# render failure.
#
# StrictUndefined makes a missing/typo'd variable raise rather than render empty
# (a baked secret silently becoming '' is a security failure). The catalog
# validator is a SEPARATE, author-facing layer that rejects StrictUndefined
# *bypass idioms* (``config.get('X')`` / ``| default``) and integration refs;
# it is NOT an SSTI gate — the sandbox is what stops injection.
# ``autoescape=False`` because these are config files (JSON/conf), not HTML.
_FILE_RENDER_ENV = ImmutableSandboxedEnvironment(
    undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
)  # globals hardened below, once _harden is defined

# The compose BODY render. Sandboxed for the same reason as the file render
# above, but with the LENIENT undefined the body has always had -- see the
# comment in ``create_compose``. Split from _FILE_RENDER_ENV so neither
# policy can be changed by accident while editing the other.
#
# IMMUTABLE, not merely sandboxed. A plain SandboxedEnvironment blocks the
# attribute walk to the interpreter but still permits mutating calls on the
# objects passed to ``render()`` -- and those are the LIVE deployment dicts.
# ``{{ volumes.update({"x": {"value": "/"}}) }}`` renders as empty output
# while adding a volume, and ``create_volumes_then_copy_files`` then runs
# ``docker container create -v /:/root`` (volume.py) and copies attacker
# content into the host filesystem. Blocking dunder traversal alone leaves
# that write primitive open, so both envs refuse mutation.
def _harden(env):
    """Drop Jinja's default globals that re-open what the sandbox closes.

    ImmutableSandboxedEnvironment refuses a MUTATING call on an immutable
    target -- but it decides that from the object the method is bound to, so
    an UNBOUND call slips past: ``dict`` is a Jinja global, and
    ``{{ dict.update(volumes.x, {"value": "/"}) }}`` checks ``dict`` (the
    class) rather than the live mapping, renders clean, and rewrites the
    context. Verified: the bound form raises SecurityError while the unbound
    form succeeded and set a volume's value to "/", which
    create_volumes_then_copy_files then mounts as ``-v /:/root``.

    ``cycler``/``joiner``/``namespace`` are the documented first hops of the
    classic escape chains, and ``lipsum``/``range`` are of no use to a compose
    file. The catalog uses none of them -- verified across every pinned entry
    -- so removing them costs nothing and shrinks the surface to what a
    compose actually needs.

    What this does NOT do is bound resource use. Removing ``range`` closes
    one way to allocate, not the class: ``*`` and ``**`` are not intercepted,
    so ``{{ "A" * (10 ** 8) }}`` is sandbox-legal and renders a 100 MB
    compose. The sandbox stops code execution; an input/output/time budget
    around the render is the second half of Feature 1 in the root
    docs/features/compose-containment/epic.md, scoped there to the custom
    (untrusted) compose path."""
    for unsafe in ('dict', 'range', 'lipsum', 'cycler', 'joiner', 'namespace'):
        env.globals.pop(unsafe, None)
    return env


# keep_trailing_newline: the per-LEAF render must preserve a value's own
# trailing \n (multi-line entrypoints, heredocs, env-file style content) —
# Jinja strips one trailing newline by default, which silently truncated
# every multi-line leaf by exactly one \n (found by adversarial review:
# 4 catalog snapshots diffed by exactly that).
_COMPOSE_RENDER_ENV = _harden(ImmutableSandboxedEnvironment(
    autoescape=False, keep_trailing_newline=True))
_harden(_FILE_RENDER_ENV)


# --- the child ---------------------------------------------------------------

_CHILD_MEM_LIMIT = 0  # set by the pool initializer in the child process


def _child_init(mem_bytes: int) -> None:
    if mem_bytes:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ImportError, OSError, ValueError):
            # macOS does not support RLIMIT_AS (setrlimit raises or is
            # ignored). The child then relies on the input/output/time caps;
            # production (alpine Linux) gets the full guard.
            pass
    # PDEATHSIG (Linux): if the PARENT dies, this child must not survive it
    # churning a full core.
    try:
        import ctypes

        # CDLL(None) resolves against the PROCESS itself: works on glibc
        # and musl (alpine) alike — "libc.so.6" does not exist on musl,
        # which silently no-oped the guard in production (verification
        # round finding).
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = getattr(libc, "prctl", None)
        if prctl is not None:
            prctl(1, 9, 0, 0, 0)  # PR_SET_PDEATHSIG, SIGKILL
    except (OSError, ValueError, AttributeError):
        # macOS has no prctl at all (dlsym raises AttributeError — which
        # once crashed the child INITIALIZER and turned every render into
        # child_died); musl/glibc Linux both resolve via CDLL(None).
        pass


def _child_render(which: str, source: str, context: dict) -> str:
    """Runs IN THE CHILD. which is 'body' or 'file'; the environments live
    in compose.py, imported lazily so a forked child pays nothing and a
    spawned one reuses the exact hardened envs (no drift to a local copy).
    """
    # The envs are MODULE-LOCAL (moved from compose.py exactly so this
    # child never imports the docker client — on spawn platforms that
    # import performs a live daemon handshake, which turned a budget
    # refusal into a DockerException 500 and re-paid 0.5s on every pool
    # rebuild).
    env = _COMPOSE_RENDER_ENV if which == "body" else _FILE_RENDER_ENV
    max_out = _knob("GREFFER_RENDER_MAX_OUTPUT", _DEFAULT_OUTPUT)
    template = env.from_string(source)  # compile-time folds happen HERE,
    # under the child's RLIMIT_AS — the case no output cap can catch.
    try:
        if not max_out:
            return template.render(**context)
        chunks = []
        total = 0
        for chunk in template.generate(**context):
            # BYTES, matching the knob's documented unit (counting
            # characters let a 1000-astral-char render pass a "1000 byte"
            # cap as 4000).
            total += len(chunk.encode("utf-8", "surrogatepass"))
            if total > max_out:
                raise RenderBudgetExceeded("output_too_large", max_out)
            chunks.append(chunk)
        return "".join(chunks)
    except RenderBudgetExceeded:
        raise
    except Exception as exc:
        # The exception is PICKLED to the parent: any implementation that
        # echoes its input into the message (a future filter edge; the
        # current set was swept and never does) would ship a giant string
        # across the boundary and re-allocate it in the parent. Re-raise
        # with a bounded message; if the type cannot be reconstructed with
        # one argument, fall back to the budget refusal.
        trimmed = str(exc)[:8192]
        # Construct BEFORE raising: a raise inside the try would be caught
        # by this very except and convert every real error into
        # render_child_error (found by the sandbox suite: SecurityError
        # must survive as SecurityError for the caller's isinstance checks).
        try:
            rebuilt = type(exc)(trimmed)
        except Exception:
            raise RenderBudgetExceeded("child_error", 8192) from exc
        raise rebuilt from None


def _child_render_tree(tree, context, which, _budget=None):
    """Runs IN THE CHILD: render every STRING LEAF of a tree value in one
    job (per-leaf submission re-pickled the entire context for every leaf
    — quadratic on real documents; codex review). which selects the env
    ('file' for json destinations, 'body' for the compose tree — the
    Feature-6 structure-preserving render).

    _budget is a one-element JOB-WIDE output accumulator: per-leaf caps
    alone let N leaves each render just under the cap, so the assembled
    document exceeded GREFFER_RENDER_MAX_OUTPUT unbounded (codex review).
    """
    if _budget is None:
        _budget = [_knob("GREFFER_RENDER_MAX_OUTPUT", _DEFAULT_OUTPUT)]
    if isinstance(tree, str):
        out = _child_render(which, tree, context)
        if _budget[0]:
            _budget[0] -= len(out.encode("utf-8", "surrogatepass"))
            if _budget[0] < 0:
                raise RenderBudgetExceeded(
                    "output_too_large",
                    _knob("GREFFER_RENDER_MAX_OUTPUT", _DEFAULT_OUTPUT))
        return out
    if isinstance(tree, dict):
        return {k: _child_render_tree(v, context, which, _budget)
                for k, v in tree.items()}
    if isinstance(tree, list):
        return [_child_render_tree(v, context, which, _budget)
                for v in tree]
    return tree


def _child_render_json(tree, context):
    return _child_render_tree(tree, context, "file")


def render_json_bounded(tree, context) -> object:
    """Budgeted render of a json-destination value's string leaves, in one
    child round-trip (input cap applies per leaf via the child render)."""
    return _run_tree_job(tree, context, "file")


def render_compose_tree_bounded(compose, context) -> object:
    """Feature 6 structure-preserving render: render every string LEAF of
    the compose tree under the body env, in ONE child job; the caller then
    yaml.dump's the result ONCE. Replacing Template(yaml.dump(compose))
    kills the round-trip fragility class structurally: yaml single-quote
    escaping (an embedded ' doubled to '') could corrupt any Jinja
    expression containing a single-quoted literal into a
    TemplateSyntaxError — and every catalog workaround-by-convention
    disappears, because values are rendered as Python strings and then
    serialized by yaml with correct quoting applied exactly once.
    Keys are NOT rendered (same intentional choice as the json walker).
    """
    return _run_tree_job(compose, context, "body")


def _run_tree_job(tree, context, which):
    src_len = sum(
        len(leaf.encode("utf-8", "surrogatepass"))
        for leaf in _iter_string_leaves(tree, keys=True))
    max_in = _knob("GREFFER_RENDER_MAX_INPUT", _DEFAULT_INPUT)
    if max_in and src_len > max_in * 100:
        # Aggregate guard for pathological leaf counts (values AND keys
        # are counted; each rendered leaf is additionally input-capped in
        # the child — keys render never, so this aggregate is their only
        # bound).
        raise RenderBudgetExceeded("input_too_large", max_in * 100)
    mem = _knob("GREFFER_RENDER_MEM_BYTES", _DEFAULT_MEM)
    timeout = _knob("GREFFER_RENDER_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT,
                    as_float=True)
    return _submit_budgeted(
        lambda pool: pool.submit(_child_render_tree, tree, context, which),
        mem, timeout)


def _iter_string_leaves(tree, keys: bool = False):
    """Yield every string leaf. keys=False (the render path) walks VALUES
    only — keys never render (an intentional hardening over the old whole-
    document template, which templated them: a structure-injection vector).
    keys=True (the budget accounting path) walks keys TOO, so a compose
    whose bulk hides in keys cannot exceed the input cap un-counted
    (review finding: a 3 MiB key passed a 1 MiB cap as 1 counted byte)."""
    if isinstance(tree, str):
        yield tree
    elif isinstance(tree, dict):
        if keys:
            for k in tree:
                if isinstance(k, str):
                    yield k
        for v in tree.values():
            yield from _iter_string_leaves(v, keys)
    elif isinstance(tree, list):
        for v in tree:
            yield from _iter_string_leaves(v, keys)


# --- the pool ----------------------------------------------------------------

_pool: concurrent.futures.ProcessPoolExecutor | None = None
_pool_mem = -1
_pool_lock = threading.Lock()


def _get_pool(mem_bytes: int) -> concurrent.futures.ProcessPoolExecutor:
    global _pool, _pool_mem
    if _pool is not None and _pool_mem == mem_bytes:
        return _pool
    with _pool_lock:
        if _pool is not None and _pool_mem == mem_bytes:
            return _pool
        if _pool is not None:
            _drop_pool_locked()
        # Pin FORK on Linux explicitly (the platform default, but now a
        # decision): the child inherits the parent's address space, so the
        # RLIMIT_AS cap effectively budgets 'parent VSZ at fork + render'.
        # Measured headroom on the prod image: ~106 MB inherited vs the
        # 256 MB default. A runtime swap to big-stack glibc would shrink
        # that headroom and make LEGITIMATE renders fail memory_exceeded —
        # re-measure if the base image changes. Spawn (macOS dev) is
        # correct too: this module imports no docker client, so the child
        # performs no daemon handshake.
        import multiprocessing

        ctx = (multiprocessing.get_context("fork")
               if sys.platform == "linux" else
               multiprocessing.get_context())
        _pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=1, initializer=_child_init, initargs=(mem_bytes,),
            mp_context=ctx)
        _pool_mem = mem_bytes
        return _pool


def _drop_pool_locked() -> None:
    global _pool, _pool_mem
    pool, _pool, _pool_mem = _pool, None, -1
    if pool is not None:
        # wait=False + cancel: the pool is discarded precisely because its
        # worker is presumed wedged or dead. shutdown() alone neither kills
        # nor abandons a BUSY worker — a timed-out render would keep
        # churning a full core with its result discarded, accumulate one
        # leaked child per wedge, and (via the executor's atexit join) stall
        # interpreter exit for as long as the churn lasts, wedging the
        # watchdog restart path. Kill the discarded pool's processes so
        # every wedge is bounded.
        # Capture the worker handles BEFORE shutdown: shutdown() may
        # clear _processes synchronously (codex review), after which the
        # kill loop would iterate nothing and the churning worker survives.
        procs = getattr(pool, "_processes", None) or {}
        procs = procs.values() if isinstance(procs, dict) else procs
        procs = list(procs)
        pool.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            try:
                proc.kill()
            except (OSError, ValueError, AttributeError):
                pass


def shutdown_render_pool() -> None:
    """Lifespan teardown hook (the shutdown_stats_pool pattern): non-daemon
    executor workers must be told to exit or interpreter exit stalls."""
    with _pool_lock:
        if _pool is not None:
            _drop_pool_locked()


def _submit_budgeted(submit, mem: int, timeout: float):
    """Shared engine: pick the pool, submit under the lock, map every
    failure to a budget refusal. Retries are BOUNDED (a pathological pool
    recycler must not pin a render — and its anyio token — forever).
    Order matters below: BrokenProcessPool IS a RuntimeError subclass, and
    CancelledError is Exception-derived but in neither production call
    site's catch tuple — both previously escaped as bare errors.
    """
    for _attempt in range(3):
        pool = _get_pool(mem)
        try:
            with _pool_lock:
                _check_pool_current(pool)
                future = submit(pool)
            return future.result(timeout=timeout or None)
        except RenderBudgetExceeded:
            raise
        except concurrent.futures.process.BrokenProcessPool as exc:
            # The child died hard (on an un-RLIMITed platform an allocation
            # blow-up is exactly this shape). MUST precede except
            # RuntimeError, of which it is a subclass — an earlier ordering
            # made this clause dead code and let the raw error escape.
            _drop_pool_and_raise(pool, "child_died", mem, exc)
        except concurrent.futures.CancelledError as exc:
            # Our future was PENDING when another caller's timeout dropped
            # this pool (shutdown(cancel_futures=True)). Retry on the fresh
            # pool; if this pool is (oddly) still current, refuse.
            if _pool is not pool:
                continue
            raise RenderBudgetExceeded("cancelled", 0) from exc
        except RuntimeError as exc:
            # Submit raced a concurrent recycle (executor shutting down).
            if _pool is pool:
                raise
            continue
        except concurrent.futures.TimeoutError as exc:
            _drop_pool_and_raise(pool, "timeout", timeout, exc)
        except MemoryError as exc:
            # Raised in the PARENT when the child hit its RLIMIT_AS and the
            # MemoryError itself was pickled back.
            _drop_pool_and_raise(pool, "memory_exceeded", mem, exc)


def render_bounded(which: str, source: str, context: dict) -> str:
    """Render ``source`` under the budget; returns the rendered string.

    Raises RenderBudgetExceeded on any cap violation (input, output, memory,
    timeout, or a dead child — all 'the render exceeded what the node will
    give it'), re-raising the caller's existing 422 mapping.
    """
    if which not in ("body", "file"):
        # A typo'd call site must fail loudly, not silently render under the
        # StrictUndefined file env (the 'else' branch of the env selection).
        raise ValueError(f"unknown render kind: {which!r}")
    max_in = _knob("GREFFER_RENDER_MAX_INPUT", _DEFAULT_INPUT)
    if max_in and len(source.encode("utf-8", "surrogatepass")) > max_in:
        raise RenderBudgetExceeded("input_too_large", max_in)
    mem = _knob("GREFFER_RENDER_MEM_BYTES", _DEFAULT_MEM)
    timeout = _knob("GREFFER_RENDER_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT,
                    as_float=True)

    return _submit_budgeted(
        lambda pool: pool.submit(_child_render, which, source, context),
        mem, timeout)


def _check_pool_current(pool) -> None:
    if _pool is not pool:
        raise RuntimeError("pool recycled")


def _drop_pool_and_raise(pool, code: str, cap, exc: Exception) -> None:
    # Recycle only the FAILED executor: another caller may have already
    # replaced the global with a healthy pool, and dropping that one
    # cancels unrelated in-flight renders as 500s (codex review).
    with _pool_lock:
        if _pool is pool:
            _drop_pool_locked()
    logger.warning("render budget: %s (cap %s) — pool recycled", code, cap)
    raise RenderBudgetExceeded(code, cap) from exc
