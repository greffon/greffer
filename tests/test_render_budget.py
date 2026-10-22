"""Tests for the render budget (compose-containment Feature 1b).

The budget is the second half of Feature 1: the sandbox stops code
execution, this stops resource exhaustion — a sandbox-legal allocation
blow-up must fail as a clean 422-shaped refusal, never take down the
--workers 1 controller that holds the manager token."""
from __future__ import annotations

import os
import sys

import pytest

from apps.utils.docker import compose, render_budget
from apps.utils.docker.render_budget import RenderBudgetExceeded


def _ctx():
    return {"id": "t1", "ports": [], "config": {}, "smtp": {}}


@pytest.fixture(autouse=True)
def _fresh_pool():
    render_budget.shutdown_render_pool()
    yield
    render_budget.shutdown_render_pool()


class TestCaps:
    def test_small_render_passes_through(self, monkeypatch):
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "100000")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "100000")
        out = render_budget.render_bounded(
            "body", "services: {{ config.a }}", {"config": {"a": "x"}})
        assert out == "services: x"

    def test_input_cap_refuses_before_any_work(self, monkeypatch):
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "64")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "0")
        source = "{{ 'pad' * 100 }}" + "# padding to exceed the cap " * 4
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded("body", source, _ctx())
        assert e.value.code == "input_too_large"
        assert "pad" not in str(e.value)  # bounded message, never content

    def test_output_cap_aborts_mid_stream(self, monkeypatch):
        # The blow-up must pass through generate(); the cap aborts on the
        # first oversized accumulation, in the CHILD (an output cap checked
        # only after a full render() would let the allocation happen first).
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "1000000")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "1024")
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded(
                "body", "{{ 'x' * 100000 }}", _ctx())
        assert e.value.code == "output_too_large"

    def test_disabled_dimensions_are_off(self, monkeypatch):
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "0")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "0")
        out = render_budget.render_bounded(
            "body", "{{ 'x' * 5000 }}", _ctx())
        assert out == "x" * 5000

    def test_exception_pickles_across_the_process_boundary(self):
        # The child raises; the parent must see RenderBudgetExceeded, not an
        # unpickling failure (the __reduce__ contract).
        import pickle

        roundtripped = pickle.loads(pickle.dumps(
            RenderBudgetExceeded("output_too_large", 99)))
        assert roundtripped.code == "output_too_large"
        assert roundtripped.cap == 99


class TestBlowups:
    def test_compile_time_fold_is_bounded(self, monkeypatch):
        # The case no output cap can catch: Jinja CONSTANT-FOLDS 'x' * 10**7
        # at COMPILE time, allocating ~10 MB before generate() yields a
        # single chunk. With a small memory cap the CHILD takes the hit
        # (MemoryError or hard death — either maps to a budget refusal) and
        # the parent converts it. On non-RLIMIT platforms (macOS) the child
        # may instead be killed -> child_died; both codes are the feature
        # working, so accept either.
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "1000000")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "1000000")
        monkeypatch.setenv("GREFFER_RENDER_MEM_BYTES", str(64 * 1024 * 1024))
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded(
                "file", "{{ 'x' * (10 ** 8) }}", _ctx())
        # On RLIMIT platforms (production alpine, Linux CI) the fold dies at
        # compile; without RLIMIT (macOS dev) the fold survives and the
        # OUTPUT cap catches the first chunk. Both are the budget working.
        assert e.value.code in ("memory_exceeded", "child_died",
                                "output_too_large")

    @pytest.mark.skipif(sys.platform == "darwin",
                       reason="RLIMIT_AS is not enforced on macOS")
    def test_memory_cap_linux(self, monkeypatch):
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "1000000")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "1000000")
        monkeypatch.setenv("GREFFER_RENDER_MEM_BYTES", str(64 * 1024 * 1024))
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded(
                "file", "{{ 'x' * (10 ** 9) }}", _ctx())
        assert e.value.code == "memory_exceeded"

    def test_timeout_path_fires_kills_worker_and_recycles_pool(self, monkeypatch):
        # A timeout so small the pool round-trip itself exceeds it: proves
        # the timeout machinery fires, the exception is the budget refusal,
        # the WORKER IS KILLED (a dropped pool must not leave a churning
        # child — the review's P1-1: leaked full-core children accumulate
        # and stall interpreter exit via the executor's atexit join), and
        # the pool is rebuilt (next render succeeds).
        import time

        assert render_budget.render_bounded("body", "ok", _ctx()) == "ok"
        pool_before = render_budget._pool
        procs = getattr(pool_before, "_processes", None) or {}
        worker = next(iter((procs.values() if isinstance(procs, dict)
                            else procs)), None)
        assert worker is not None
        monkeypatch.setenv("GREFFER_RENDER_TIMEOUT_SECONDS", "0.0001")
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded("body", "ok", _ctx())
        assert e.value.code == "timeout"
        monkeypatch.delenv("GREFFER_RENDER_TIMEOUT_SECONDS")
        # The discarded pool's worker is dead within a bounded wait.
        # (multiprocessing BaseProcess exposes is_alive(); poll() is
        # subprocess-only.)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and worker.is_alive():
            time.sleep(0.05)
        assert not worker.is_alive(), "timed-out worker still alive"
        # Belt: the PID itself is gone (is_alive can be False for a zombie
        # that shutdown() later reaps; the kill must have actually fired).
        try:
            os.kill(worker.pid, 0)
            raise AssertionError("worker pid still signalable after kill")
        except (ProcessLookupError, OSError):
            pass
        # A fresh pool serves the next render.
        assert render_budget.render_bounded("body", "ok", _ctx()) == "ok"
        assert render_budget._pool is not pool_before

    def test_hard_child_death_is_child_died_not_raw_broken_pool(self):
        # Regression (found by the verification round): except RuntimeError
        # sat ABOVE except BrokenProcessPool (a RuntimeError SUBCLASS),
        # making the child_died clause dead code — an externally killed
        # worker escaped as a raw BrokenProcessPool 500 on the baked/json
        # paths instead of the designed refusal.
        import signal

        assert render_budget.render_bounded("body", "ok", _ctx()) == "ok"
        pool = render_budget._pool
        procs = getattr(pool, "_processes", None) or {}
        worker = next(iter((procs.values() if isinstance(procs, dict)
                            else procs)))
        os.kill(worker.pid, signal.SIGKILL)
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded("body", "ok", _ctx())
        assert e.value.code in ("child_died", "cancelled")

    def test_cancelled_pending_future_does_not_escape(self, monkeypatch):
        # Regression: a PENDING future cancelled by another caller's pool
        # drop raised bare CancelledError (not in any catch tuple → 500 on
        # the baked/json paths). The engine must convert or retry it.
        import threading

        monkeypatch.setenv("GREFFER_RENDER_TIMEOUT_SECONDS", "0.0001")
        errors = []

        def _render():
            try:
                render_budget.render_bounded("body", "ok", _ctx())
            except RenderBudgetExceeded:
                errors.append("budget")
            except Exception as exc:  # noqa: BLE001 — the escape under test
                errors.append(("ESCAPED", type(exc).__name__))

        threads = [threading.Thread(target=_render) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert all(e == "budget" for e in errors), errors

    def test_single_quoted_jinja_literal_survives(self, monkeypatch, tmp_path):
        # Feature 6 round-trip guard: yaml.dump single-quote doubling (an
        # embedded ' -> '') corrupted any Jinja expression containing a
        # single-quoted literal into a TemplateSyntaxError under the old
        # Template(yaml.dump(compose)) pipeline. Structure-preserving
        # rendering must render it cleanly.
        monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
        from apps.utils.docker.compose import create_compose

        compose = {"services": {"app": {
            "image": "x",
            "environment": {
                "X": "{{ 'fallback' if not instance_url else 'set' }}",
            }}}}
        info = {"id": "rt1", "ports": [], "configurations": [],
                "integrations": {}}
        create_compose(compose, info)
        import yaml as _yaml

        rendered = _yaml.safe_load(open(
            tmp_path / "rt1" / "docker-compose.yml").read())
        value = rendered["services"]["app"]["environment"]["X"]
        assert value in ("fallback", "set"), value

    def test_leaf_trailing_newline_preserved(self):
        # Regression (adversarial review): Jinja strips one trailing
        # newline by default; per-leaf rendering silently truncated every
        # multi-line value by exactly one \n (4 catalog snapshots diffed).
        from apps.utils.docker.render_budget import _child_render

        out = _child_render("body", "line1\nline2\n", {})
        assert out == "line1\nline2\n", repr(out)

    def test_keys_count_toward_input_cap(self, monkeypatch):
        # Regression: the budget walked values only, so a compose whose
        # bulk lived in KEYS passed the input cap un-counted.
        # The aggregate guard is max_input * 100, so the key bulk must
        # exceed THAT to trip (the cap semantics for tree jobs).
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "8")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "0")
        tree = {"k" * (8 * 101): "v"}
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_compose_tree_bounded(tree, {})
        assert e.value.code == "input_too_large"

    def test_unknown_render_kind_refused(self):
        with pytest.raises(ValueError, match="unknown render kind"):
            render_budget.render_bounded("bodyy", "ok", _ctx())

    def test_multibyte_output_counts_bytes(self, monkeypatch):
        # Regression: the cap counted CHARACTERS; astral-plane content
        # passed a '1000 byte' cap as 4000 actual bytes.
        monkeypatch.setenv("GREFFER_RENDER_MAX_INPUT", "100000")
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "1000")
        with pytest.raises(RenderBudgetExceeded) as e:
            render_budget.render_bounded("body", "{{ '\U0001F600' * 400 }}", _ctx())
        assert e.value.code == "output_too_large"


class TestWiring:
    """The two production call sites map budget refusals to their existing
    422 shapes (ConfigRenderError), without new router wiring."""

    def test_body_render_budget_maps_to_config_render_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
        # Feature 6 made the body a TREE job: the input cap is aggregate
        # (max_in * 100 over keys+values), so trip the JOB-WIDE OUTPUT cap
        # instead — the wiring assertion (budget -> ConfigRenderError) is
        # what this test pins.
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "8")
        with pytest.raises(compose.ConfigRenderError, match="output_too_large"):
            compose.create_compose(
                {"services": {"a": {"image": "x",
                                     "environment": {"K": "vvvvvvvvvv"}}}},
                {"id": "t1", "ports": [], "configurations": [],
                 "integrations": {}})

    def test_baked_file_budget_maps_to_config_render_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
        monkeypatch.setenv("GREFFER_RENDER_MAX_OUTPUT", "64")
        info = {"id": "t1", "ports": [], "configurations": [{
            "value": {"file": "data:application/octet-stream;base64,aGVsbG8="},
            "destinations": [{
                "type": "file", "name": "big.txt", "volume": "v",
                "x-greffon-render": True}]}],
            "volumes": {"v": {"name": "v", "value": "v",
                              "containers": {}, "files": []}}}
        # The baked payload renders {{ 'x' * N }} -> output cap -> 422.
        import base64
        payload = base64.b64encode(b"{{ 'x' * 10000 }}").decode()
        info["configurations"][0]["value"]["file"] = (
            "data:application/octet-stream;base64," + payload)
        with pytest.raises(compose.ConfigRenderError,
                           match="output_too_large"):
            compose.apply_configuration(info, {})
