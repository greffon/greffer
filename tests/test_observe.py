"""Tests for per-instance observability digests (resource-monitoring epic,
Feature 2): strict enumeration, stats/disk digests, and the TTL caches."""
from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest

from apps.utils.docker import observe


def _container(name="i1_web_1", status="running", project="i1",
               service="web", ignore=False, stats=None):
    c = Mock()
    c.name = name
    c.status = status
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": service,
    }
    if ignore:
        labels["com.greffon.status"] = "ignore"
    c.labels = labels
    c.stats.return_value = stats if stats is not None else {}
    return c


def _running_stats(total=200, pre_total=100, sys=2000, pre_sys=1000,
                   online=2, usage=500, cache=100, limit=1000,
                   rx=10, tx=20, blk_read=4, blk_write=8):
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": total},
            "system_cpu_usage": sys,
            "online_cpus": online,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": pre_total},
            "system_cpu_usage": pre_sys,
        },
        "memory_stats": {
            "usage": usage,
            "limit": limit,
            "stats": {"inactive_file": cache},
        },
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": blk_read},
                {"op": "Write", "value": blk_write},
            ]
        },
    }


@pytest.fixture(autouse=True)
def _clear_caches():
    observe._stats_cache.clear()
    observe._disk_cache.clear()
    observe._df_cache.update({"at": 0.0, "volumes": None})
    yield


# --- enumeration --------------------------------------------------------

def test_list_instance_containers_uses_exact_project_label():
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = []
        observe.list_instance_containers("abc-123")
    cl.containers.list.assert_called_once_with(
        all=True,
        filters={"label": "com.docker.compose.project=abc-123"},
    )


def test_list_instance_containers_excludes_ignore_label():
    web = _container(name="i1_web_1")
    sidecar = _container(name="i1_migrate_1", service="migrate", ignore=True)
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = [web, sidecar]
        result = observe.list_instance_containers("i1")
    assert result == [web]  # the ignore-labelled one-shot is dropped


def test_list_instance_containers_keeps_tenant_named_migrate():
    # A legitimate tenant container literally named *migrate* (no ignore label)
    # must still be reported (the bug the unanchored substring would cause).
    mig = _container(name="i1_migrate_1", service="migrate", ignore=False)
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = [mig]
        result = observe.list_instance_containers("i1")
    assert result == [mig]


# --- stats digest -------------------------------------------------------

def test_cpu_percent_computed():
    # cpu_delta=100, sys_delta=1000, online=2 -> 100/1000*2*100 = 20.0
    assert observe._cpu_percent(_running_stats()) == 20.0


def test_cpu_percent_cold_start_sentinel():
    cold = _running_stats()
    cold["precpu_stats"]["system_cpu_usage"] = cold["cpu_stats"][
        "system_cpu_usage"]  # sys_delta == 0
    assert observe._cpu_percent(cold) == 0.0


def test_cpu_percent_not_clamped_to_100():
    # 4 cores fully busy -> ~400, NOT clamped (per-container is multi-core).
    s = _running_stats(total=1100, pre_total=100, sys=2000, pre_sys=1000,
                       online=4)
    assert observe._cpu_percent(s) == 400.0


def test_mem_subtracts_reclaimable_cache():
    used, limit = observe._mem(_running_stats())
    assert used == 400  # usage 500 - inactive_file 100
    assert limit == 1000


def test_net_and_blk_summed():
    s = _running_stats()
    assert observe._net(s) == (10, 20)
    assert observe._blk(s) == (4, 8)


def test_net_none_without_interfaces():
    s = _running_stats()
    del s["networks"]
    assert observe._net(s) == (None, None)


def _deploy(tmp_path, instance_id="i1"):
    d = tmp_path / instance_id
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}\n")


def test_instance_stats_none_when_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    assert observe.instance_stats("never-deployed") is None


def test_instance_stats_running_and_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    running = _container(name="i1_web_1", status="running",
                         stats=_running_stats())
    stopped = _container(name="i1_db_1", status="exited", service="db")
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = [running, stopped]
        body = observe.instance_stats("i1")
    assert body["instance_id"] == "i1"
    web = next(c for c in body["containers"] if c["service"] == "web")
    db = next(c for c in body["containers"] if c["service"] == "db")
    assert web["state"] == "running"
    assert web["cpu_percent"] == 20.0
    assert web["mem_used_bytes"] == 400
    assert web["net_rx_bytes"] == 10
    assert db["state"] == "exited"
    assert db["cpu_percent"] is None  # non-running -> null metrics, no error
    assert db["mem_used_bytes"] is None
    stopped.stats.assert_not_called()  # never even read stats on a stopped one


def test_instance_stats_stats_failure_yields_null_not_500(tmp_path,
                                                          monkeypatch):
    import docker
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    c = _container(name="i1_web_1", status="running")
    c.stats.side_effect = docker.errors.APIError("boom")
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = [c]
        body = observe.instance_stats("i1")
    assert body["containers"][0]["cpu_percent"] is None


def test_instance_stats_preserves_container_order(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    names = [f"i1_svc{i}_1" for i in range(6)]
    containers = [_container(name=n, service=f"svc{i}",
                             stats=_running_stats())
                  for i, n in enumerate(names)]
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = containers
        body = observe.instance_stats("i1")
    # The parallel fan-out must keep results aligned to enumeration order.
    assert [c["name"] for c in body["containers"]] == names


def test_instance_stats_reads_containers_concurrently(tmp_path, monkeypatch):
    """The per-container daemon reads must run in PARALLEL. A barrier that only
    releases once N threads are simultaneously inside ``stats()`` proves it
    deterministically: a serial read would deadlock the barrier (only ever one
    thread inside) and raise BrokenBarrierError on timeout."""
    import threading
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    n = 6
    barrier = threading.Barrier(n, timeout=5)

    def _concurrent_stats(*_a, **_k):
        barrier.wait()  # blocks until all N threads are inside at once
        return _running_stats()

    containers = []
    for i in range(n):
        c = _container(name=f"i1_svc{i}_1", service=f"svc{i}")
        c.stats.side_effect = _concurrent_stats
        containers.append(c)
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = containers
        body = observe.instance_stats("i1")
    # All N digested with real metrics (barrier released => true concurrency).
    assert len(body["containers"]) == n
    assert all(c["cpu_percent"] == 20.0 for c in body["containers"])


def test_instance_stats_slow_container_times_out_to_null(tmp_path, monkeypatch):
    """A container whose read exceeds the digest deadline degrades to null
    metrics (state preserved) without holding back the fast containers."""
    import time
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    monkeypatch.setattr(observe, "_STATS_DEADLINE_SECONDS", 0.2)
    _deploy(tmp_path)
    fast = _container(name="i1_web_1", service="web", stats=_running_stats())
    slow = _container(name="i1_db_1", service="db")
    slow.stats.side_effect = lambda *_a, **_k: (time.sleep(1.0)
                                                or _running_stats())
    with patch.object(observe, "client") as cl:
        cl.containers.list.return_value = [fast, slow]
        start = time.monotonic()
        body = observe.instance_stats("i1")
        elapsed = time.monotonic() - start
    web = next(c for c in body["containers"] if c["service"] == "web")
    db = next(c for c in body["containers"] if c["service"] == "db")
    assert web["cpu_percent"] == 20.0          # fast one digested
    assert db["state"] == "running"            # state preserved (enumeration)
    assert db["cpu_percent"] is None           # slow one degraded to null
    assert elapsed < 1.0                        # returned at the deadline


# --- disk digest --------------------------------------------------------

def test_instance_disk_filters_by_anchored_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    (tmp_path / "i1" / "f.txt").write_bytes(b"x" * 100)
    df = {"Volumes": [
        {"Name": "i1_db_data", "UsageData": {"Size": 5000}},
        {"Name": "i2_db_data", "UsageData": {"Size": 9999}},  # other tenant
    ]}
    with patch.object(observe, "client") as cl:
        cl.df.return_value = df
        body = observe.instance_disk("i1")
    # app_dir_bytes is the real walked size (the 100-byte f.txt plus the
    # rendered docker-compose.yml), so assert it relationally, not hardcoded.
    assert body["app_dir_bytes"] >= 100
    assert body["volumes_bytes"] == 5000
    assert body["total_bytes"] == body["app_dir_bytes"] + 5000
    names = [v["name"] for v in body["volumes"]]
    assert names == ["i1_db_data"]  # i2's volume never appears


def test_instance_disk_null_when_volume_size_unavailable(tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    df = {"Volumes": [{"Name": "i1_db_data", "UsageData": {"Size": -1}}]}
    with patch.object(observe, "client") as cl:
        cl.df.return_value = df
        body = observe.instance_disk("i1")
    assert body["volumes_bytes"] is None  # never a bogus 0
    assert body["total_bytes"] is None
    assert body["volumes"][0]["bytes"] is None


def test_instance_disk_none_when_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    assert observe.instance_disk("never") is None


def test_df_snapshot_shared_across_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path, "i1")
    _deploy(tmp_path, "i2")
    df = {"Volumes": [{"Name": "i1_x", "UsageData": {"Size": 1}}]}
    with patch.object(observe, "client") as cl:
        cl.df.return_value = df
        observe.instance_disk("i1")
        observe.instance_disk("i2")
        assert cl.df.call_count == 1  # one host-wide walk shared across both


# --- TTL caches ---------------------------------------------------------

def test_cached_stats_one_fanout_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    c = _container(name="i1_web_1", status="running", stats=_running_stats())
    with patch.object(observe, "client") as cl, \
            patch.object(observe.time, "monotonic", return_value=1000.0):
        cl.containers.list.return_value = [c]
        first = observe.cached_instance_stats("i1")
        second = observe.cached_instance_stats("i1")
    assert first == second  # byte-identical cached body
    assert cl.containers.list.call_count == 1  # one fan-out


def test_cached_stats_reinvokes_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    c = _container(name="i1_web_1", status="running", stats=_running_stats())
    with patch.object(observe, "client") as cl, \
            patch.object(observe.time, "monotonic") as mono:
        cl.containers.list.return_value = [c]
        mono.return_value = 1000.0
        observe.cached_instance_stats("i1")
        mono.return_value = 1000.0 + observe._STATS_TTL_SECONDS + 1
        observe.cached_instance_stats("i1")
    assert cl.containers.list.call_count == 2  # re-walked after TTL
