"""Tests for the instance resource-limit telemetry (compose-containment
Feature 4, stage 1): size parsing, env fallbacks, and the over-limit
comparison — including the rate limit that keeps a steady over-limit app
from spamming the log on every pull."""

from __future__ import annotations

import logging

import pytest

from apps.utils.docker import limits


@pytest.fixture(autouse=True)
def _greffer_log_visible(caplog):
    """app/logging.py configures the ``greffer`` logger with
    ``propagate: False``; any test that imported the app earlier in the
    suite leaves that set, and caplog (root-handler based) then sees nothing.
    Force propagation for the duration of each test here."""
    lg = logging.getLogger("greffer")
    prev = lg.propagate
    lg.propagate = True
    yield
    lg.propagate = prev


@pytest.fixture(autouse=True)
def _clean_limits_cache():
    """Reset (and restore) the process-limits cache and the rate-limit map
    around every test, the test_observe.py ``_clear_caches`` convention: a
    test that sets env vars and forgets the cache would poison later suite
    files that call ``instance_stats`` with values computed from the fake
    env, and a leaked (instance, service) warn key silently suppresses the
    next test's first warning."""
    saved_cache = limits._limits_cache
    limits._limits_cache = None
    limits._last_warned.clear()
    yield
    limits._limits_cache = saved_cache


def _entry(service="web", mem=None, cpu=None):
    return {
        "service": service,
        "name": f"i1_{service}_1",
        "state": "running",
        "mem_used_bytes": mem,
        "cpu_percent": cpu,
    }


def _limits(mem=1024, cpus=1.0, pids=128):
    return limits.InstanceLimits(mem_bytes=mem, cpus=cpus, pids=pids)


class TestParseSize:
    def test_plain_bytes(self):
        assert limits.parse_size("512") == 512

    def test_suffixes(self):
        assert limits.parse_size("1k") == 1024
        assert limits.parse_size("10m") == 10 * 1024**2
        assert limits.parse_size("2g") == 2 * 1024**3
        assert limits.parse_size("1t") == 1024**4

    def test_decimal(self):
        assert limits.parse_size("1.5g") == int(1.5 * 1024**3)

    def test_two_letter_suffixes(self):
        # Regression: '512mb' is valid docker syntax and previously fell
        # back to the default, silently disabling the operator's threshold.
        assert limits.parse_size('512mb') == 512 * 1024 ** 2
        assert limits.parse_size('2gb') == 2 * 1024 ** 3
        assert limits.parse_size('1kb') == 1024
        assert limits.parse_size('512mB') == 512 * 1024 ** 2

    def test_case_and_whitespace_insensitive(self):
        assert limits.parse_size(" 2G ") == 2 * 1024**3

    def test_garbage_returns_none(self):
        assert limits.parse_size("bananas") is None
        assert limits.parse_size("1g512m") is None  # compound unsupported, loudly
        assert limits.parse_size("") is None
        assert limits.parse_size("-5m") is None
        assert limits.parse_size(None) is None


class TestComputedLimits:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("GREFFER_INSTANCE_MEM_LIMIT", raising=False)
        monkeypatch.delenv("GREFFER_INSTANCE_CPUS", raising=False)
        monkeypatch.delenv("GREFFER_INSTANCE_PIDS", raising=False)
        got = limits.computed_instance_limits()
        assert got.mem_bytes == 2 * 1024**3
        assert got.cpus == 2.0
        assert got.pids == 512

    def test_env_overrides_and_off(self, monkeypatch):
        monkeypatch.setenv("GREFFER_INSTANCE_MEM_LIMIT", "512m")
        monkeypatch.setenv("GREFFER_INSTANCE_CPUS", "4")
        monkeypatch.setenv("GREFFER_INSTANCE_PIDS", "0")  # explicit off
        got = limits.computed_instance_limits()
        assert got.mem_bytes == 512 * 1024**2
        assert got.cpus == 4.0
        assert got.pids is None

    def test_mem_empty_string_disables(self, monkeypatch):
        monkeypatch.setenv("GREFFER_INSTANCE_MEM_LIMIT", "")
        assert limits.computed_instance_limits().mem_bytes is None

    def test_typo_falls_back_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("GREFFER_INSTANCE_MEM_LIMIT", "2x")
        monkeypatch.setenv("GREFFER_INSTANCE_CPUS", "lots")
        monkeypatch.setenv("GREFFER_INSTANCE_PIDS", "many")
        with caplog.at_level(logging.WARNING, logger="greffer"):
            got = limits.computed_instance_limits()
        assert got.mem_bytes == 2 * 1024**3
        assert got.cpus == 2.0
        assert got.pids == 512
        assert len([r for r in caplog.records if "GREFFER_INSTANCE" in r.message]) == 3


class TestCheckLimitsObserved:
    def test_mem_over_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1", [_entry(mem=2048, cpu=10.0)], _limits(), now=1000.0
            )
        assert n == 1
        assert any("instance_limit_exceeded" in r.message for r in caplog.records)

    def test_cpu_over_warns_multicore_scale(self, caplog):
        # cpus=2.0 -> threshold 200 percent; a busy 4-core container reads ~400
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1", [_entry(mem=1, cpu=350.0)], _limits(cpus=2.0), now=1000.0
            )
        assert n == 1

    def test_under_limit_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1", [_entry(mem=512, cpu=50.0)], _limits(), now=1000.0
            )
        assert n == 0
        assert not caplog.records

    def test_null_metrics_skipped(self, caplog):
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1", [_entry(mem=None, cpu=None)], _limits(), now=1000.0
            )
        assert n == 0
        assert not caplog.records

    def test_rate_limited_per_service(self, caplog):
        digest = [_entry(mem=2048), _entry(service="db", mem=2048)]
        with caplog.at_level(logging.WARNING, logger="greffer"):
            assert (
                limits.check_limits_observed("i1", digest, _limits(), now=1000.0) == 2
            )
            # Second poll 60s later: still exceeding, but inside the re-warn
            # window -> counted, NOT re-logged.
            assert (
                limits.check_limits_observed("i1", digest, _limits(), now=1060.0) == 2
            )
            # Past the window -> both warn again.
            assert (
                limits.check_limits_observed(
                    "i1", digest, _limits(), now=1000.0 + 601.0
                )
                == 2
            )
        warned = [
            r.message for r in caplog.records if "instance_limit_exceeded" in r.message
        ]
        assert len(warned) == 4  # 2 + 0 + 2

    def test_disabled_dimensions_are_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1",
                [_entry(mem=10**12, cpu=10**6)],
                _limits(mem=None, cpus=None),
                now=1000.0,
            )
        assert n == 0
        assert not caplog.records

    def test_telemetry_failure_never_raises(self, monkeypatch, caplog):
        # The observe.py hook wraps this pass in try/except; verify the pass
        # itself tolerates malformed entries rather than relying on that.
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1",
                [{"service": None, "mem_used_bytes": "lots"}],
                _limits(),
                now=1000.0,
            )
        assert n == 0

    def test_first_warn_fires_right_after_boot(self, caplog):
        # Regression: a 0.0 "never warned" sentinel suppressed the FIRST
        # warning while monotonic() < 600 — the first ten minutes after a
        # host boot, exactly when a fresh node's telemetry matters.
        with caplog.at_level(logging.WARNING, logger="greffer"):
            n = limits.check_limits_observed(
                "i1", [_entry(mem=2048)], _limits(), now=100.0
            )
        assert n == 1
        assert any("instance_limit_exceeded" in r.message for r in caplog.records)

    def test_concurrent_prune_snapshot_no_runtimeerror(self):
        # Regression: pruning iterated the LIVE dict view; a concurrent
        # digest inserting a key mid-iteration raised RuntimeError (dropping
        # that poll's telemetry). The snapshot must make it benign.
        import sys
        import threading

        # Shrink the switch interval so the inserter thread preempts mid-
        # comprehension: at the default interval a revert of the snapshot to
        # a live view survives this test undetected (0/50 in verification).
        # Restored in the finally: the interval is interpreter-wide and must
        # not leak into later threaded tests (codex review).
        prev_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        stop = threading.Event()
        errors: list[Exception] = []

        def inserter():
            i = 0
            while not stop.is_set():
                limits._last_warned[(f"i{i % 7}", f"s{i % 5}")] = 0.0
                i += 1

        t = threading.Thread(target=inserter)
        t.start()
        try:
            for _ in range(200):
                limits._prune_last_warned(10_000.0)
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)
        finally:
            stop.set()
            t.join()
            sys.setswitchinterval(prev_interval)
        assert not errors


class TestParseSizeEdgeCases:
    def test_overflow_returns_none_not_crash(self):
        # Regression: int(inf) raised OverflowError out of a "never raises"
        # parser, crash-looping boot via the Settings validator.
        assert limits.parse_size("1e999g") is None
        assert limits.parse_size("1e999") is None

    def test_mem_zero_means_off(self, monkeypatch):
        # Regression: cpus/pids treated 0 as off but mem kept a literal
        # 0-byte limit that everything "exceeds" — docker's --memory=0 means
        # unlimited, and the calibration data must not read warn-everything.
        monkeypatch.setenv("GREFFER_INSTANCE_MEM_LIMIT", "0")
        assert limits.computed_instance_limits().mem_bytes is None

    def test_cpus_nan_falls_back(self, monkeypatch, caplog):
        # Regression: float('nan') passed the < 0 check and nan > x is always
        # False, silently disabling the CPU dimension with no warning.
        monkeypatch.setenv("GREFFER_INSTANCE_CPUS", "nan")
        with caplog.at_level(logging.WARNING, logger="greffer"):
            got = limits.computed_instance_limits()
        assert got.cpus == 2.0
        assert any("GREFFER_INSTANCE_CPUS" in r.message for r in caplog.records)

    def test_mem_overflow_env_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("GREFFER_INSTANCE_MEM_LIMIT", "1e300g")
        with caplog.at_level(logging.WARNING, logger="greffer"):
            got = limits.computed_instance_limits()
        assert got.mem_bytes == 2 * 1024**3
        assert any("GREFFER_INSTANCE_MEM_LIMIT" in r.message for r in caplog.records)


class TestSettingsCoercion:
    """The three Settings knobs follow the typo-must-not-crash rule; these
    cover the validators that had zero coverage when the boot crash and the
    Settings/limits divergence were found."""

    def _settings(self, monkeypatch, **env):
        monkeypatch.setenv("GREFFER_ID", "x")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.settings import Settings

        return Settings()

    def test_mem_typo_and_overflow_fall_back(self, monkeypatch):
        s = self._settings(monkeypatch, GREFFER_INSTANCE_MEM_LIMIT="1e999g")
        assert s.greffer_instance_mem_limit == "2g"
        s = self._settings(monkeypatch, GREFFER_INSTANCE_MEM_LIMIT="2x")
        assert s.greffer_instance_mem_limit == "2g"

    def test_mem_empty_and_valid_pass_through(self, monkeypatch):
        s = self._settings(monkeypatch, GREFFER_INSTANCE_MEM_LIMIT="")
        assert s.greffer_instance_mem_limit == ""
        s = self._settings(monkeypatch, GREFFER_INSTANCE_MEM_LIMIT="4g")
        assert s.greffer_instance_mem_limit == "4g"

    def test_cpus_garbage_negative_nan_fall_back(self, monkeypatch):
        for bad in ("lots", "-1", "nan", "inf"):
            s = self._settings(monkeypatch, GREFFER_INSTANCE_CPUS=bad)
            assert s.greffer_instance_cpus == 2.0, bad

    def test_pids_garbage_negative_fall_back(self, monkeypatch):
        for bad in ("many", "-5"):
            s = self._settings(monkeypatch, GREFFER_INSTANCE_PIDS=bad)
            assert s.greffer_instance_pids == 512, bad

    def test_defaults_match_limits_module(self, monkeypatch):
        # The single-source contract: Settings defaults ARE limits.py's
        # constants. If these drift apart the divergence review finding
        # comes back.
        s = self._settings(monkeypatch)
        assert s.greffer_instance_mem_limit == limits.DEFAULT_MEM_LIMIT
        assert s.greffer_instance_cpus == float(limits.DEFAULT_CPUS)
        assert s.greffer_instance_pids == int(limits.DEFAULT_PIDS)
