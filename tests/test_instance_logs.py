"""Tests for bounded per-instance log reads (resource-monitoring epic,
Feature 2, logs slice): cursor encode/validate, container de-dup, merged
per-container positions, deploy-log offset + rotation + truncation."""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from apps.utils.docker import instance_logs as il


def _container(service="web", name="i1_web_1", raw=b""):
    c = Mock()
    c.name = name
    c.labels = {"com.docker.compose.service": service}
    c.logs.return_value = raw
    return c


def _deploy(tmp_path, instance_id="i1", content=b""):
    d = tmp_path / instance_id
    d.mkdir(exist_ok=True)
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / "deploy.log").write_bytes(content)


# --- cursor -------------------------------------------------------------

def test_clamp_tail_bounds():
    assert il.clamp_tail("5") == 5
    assert il.clamp_tail(0) == 1
    assert il.clamp_tail(10_000) == il.LOG_TAIL_MAX
    assert il.clamp_tail("garbage") == il.LOG_TAIL_DEFAULT
    assert il.clamp_tail(None) == il.LOG_TAIL_DEFAULT


def test_cursor_round_trip():
    token = il._encode_cursor({"v": 1, "ts": "2026-06-15T14:00:00Z"})
    assert il.decode_cursor(token) == {"v": 1, "ts": "2026-06-15T14:00:00Z"}


def test_decode_cursor_none_is_none():
    assert il.decode_cursor(None) is None
    assert il.decode_cursor("") is None


def test_decode_cursor_rejects_garbage():
    with pytest.raises(il.BadCursor):
        il.decode_cursor("!!!not-base64!!!")


def test_decode_cursor_rejects_wrong_version():
    bad = il._encode_cursor({"v": 2, "ts": "x"})
    with pytest.raises(il.BadCursor):
        il.decode_cursor(bad)


def test_decode_cursor_rejects_forged_field_types():
    # A decodable-but-forged cursor (wrong field types / negative offset) must
    # be a clean BadCursor (-> 400), never a 500 downstream.
    for bad in ({"v": 1, "off": "abc"}, {"v": 1, "off": -5},
                {"v": 1, "off": [1]}, {"v": 1, "ts": 123},
                {"v": 1, "positions": {"web": 5}}):
        with pytest.raises(il.BadCursor):
            il.decode_cursor(il._encode_cursor(bad))


# --- deploy stream ------------------------------------------------------

def test_deploy_reads_complete_lines_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path, content=b"line one\nline two\npartial-no-newline")
    body = il.instance_logs("i1", "deploy", 100, None)
    msgs = [ln["msg"] for ln in body["lines"]]
    assert msgs == ["line one", "line two"]  # the partial line is held back
    assert body["truncated"] is False


def test_deploy_cursor_advances_without_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path, content=b"a\nb\n")
    first = il.instance_logs("i1", "deploy", 100, None)
    assert [ln["msg"] for ln in first["lines"]] == ["a", "b"]
    # Append and follow from the cursor: only the new line, no overlap.
    (tmp_path / "i1" / "deploy.log").write_bytes(b"a\nb\nc\n")
    second = il.instance_logs("i1", "deploy", 100, first["next_cursor"])
    assert [ln["msg"] for ln in second["lines"]] == ["c"]


def test_deploy_detects_redeploy_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path, content=b"old-deploy-line-1\nold-deploy-line-2\n")
    first = il.instance_logs("i1", "deploy", 100, None)
    # A redeploy truncates deploy.log ('wb') to a shorter content; the cursor
    # offset now exceeds the file size, so we reset and flag rotated.
    (tmp_path / "i1" / "deploy.log").write_bytes(b"new\n")
    second = il.instance_logs("i1", "deploy", 100, first["next_cursor"])
    assert second["rotated"] is True
    assert [ln["msg"] for ln in second["lines"]] == ["new"]


def test_deploy_truncated_when_over_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path, content=b"1\n2\n3\n4\n5\n")
    body = il.instance_logs("i1", "deploy", 2, None)
    assert [ln["msg"] for ln in body["lines"]] == ["1", "2"]
    assert body["truncated"] is True
    # next_cursor resumes exactly after the emitted boundary (no gap).
    nxt = il.instance_logs("i1", "deploy", 2, body["next_cursor"])
    assert [ln["msg"] for ln in nxt["lines"]] == ["3", "4"]


def test_deploy_none_when_no_log_and_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    assert il.instance_logs("never", "deploy", 100, None) is None


# --- container / all streams -------------------------------------------

_LINES = (b"2026-06-15T14:03:21.004000000Z hello\n"
          b"2026-06-15T14:03:22.005000000Z world\n")


def test_container_initial_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    c = _container(raw=_LINES)
    with patch.object(il, "instance_is_deployed", return_value=True), \
            patch.object(il, "list_instance_containers", return_value=[c]):
        body = il.instance_logs("i1", "container", 100, None)
    assert [ln["msg"] for ln in body["lines"]] == ["hello", "world"]
    # initial load requested a tail window, not a since.
    assert c.logs.call_args.kwargs["tail"] == 100
    assert "since" not in c.logs.call_args.kwargs


def test_container_follow_dedups_strictly_after_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    c = _container(raw=_LINES)
    with patch.object(il, "instance_is_deployed", return_value=True), \
            patch.object(il, "list_instance_containers", return_value=[c]):
        first = il.instance_logs("i1", "container", 100, None)
        # Second poll returns the SAME bytes from docker, but the cursor must
        # de-dup so zero lines overlap for a quiescent log.
        second = il.instance_logs("i1", "container", 100,
                                  first["next_cursor"])
    assert second["lines"] == []


def test_all_merges_and_keeps_per_container_positions(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    web = _container("web", "i1_web_1",
                     b"2026-06-15T14:03:21.000000000Z w1\n")
    db = _container("db", "i1_db_1",
                    b"2026-06-15T14:03:20.000000000Z d1\n")
    with patch.object(il, "instance_is_deployed", return_value=True), \
            patch.object(il, "list_instance_containers",
                         return_value=[web, db]):
        body = il.instance_logs("i1", "all", 100, None)
    # merged + sorted by ts: db (14:03:20) before web (14:03:21)
    assert [ln["msg"] for ln in body["lines"]] == ["d1", "w1"]
    cur = il.decode_cursor(body["next_cursor"])
    assert set(cur["positions"]) == {"web", "db"}


def test_multi_container_stream_container_no_line_loss(tmp_path, monkeypatch):
    # The scalar-watermark bug: a lagging container's new line (below another
    # container's high-water mark) must NOT be dropped. Per-container positions
    # fix it; this test fails under a single scalar `ts` cursor.
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    web = _container("web", "i1_web_1",
                     b"2026-06-15T14:03:05.000000000Z w-old\n")
    db = _container("db", "i1_db_1",
                    b"2026-06-15T14:03:09.000000000Z d-old\n")
    with patch.object(il, "instance_is_deployed", return_value=True), \
            patch.object(il, "list_instance_containers",
                         return_value=[web, db]):
        first = il.instance_logs("i1", "container", 100, None)
        # web emits a NEW line at :07, BELOW db's :09 high-water mark.
        web.logs.return_value = (
            b"2026-06-15T14:03:05.000000000Z w-old\n"
            b"2026-06-15T14:03:07.000000000Z w-new\n")
        second = il.instance_logs("i1", "container", 100,
                                  first["next_cursor"])
    msgs = [ln["msg"] for ln in second["lines"]]
    assert "w-new" in msgs       # lagging container's new line preserved
    assert "w-old" not in msgs   # de-duped per container
    assert "d-old" not in msgs   # db had nothing new


def test_service_selector_narrows_to_one_container(tmp_path, monkeypatch):
    # ?service=web returns ONLY the web container's lines, not the merged view.
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    web = _container("web", "i1_web_1",
                     b"2026-06-15T14:03:05.000000000Z w1\n")
    db = _container("db", "i1_db_1",
                    b"2026-06-15T14:03:06.000000000Z d1\n")
    with patch.object(il, "instance_is_deployed", return_value=True), \
            patch.object(il, "list_instance_containers",
                         return_value=[web, db]):
        body = il.instance_logs("i1", "container", 100, None, service="web")
    assert [ln["msg"] for ln in body["lines"]] == ["w1"]
    assert all(ln["service"] == "web" for ln in body["lines"])
    db.logs.assert_not_called()  # the unselected container is never read


def test_service_selector_unknown_service_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    _deploy(tmp_path)
    web = _container("web", "i1_web_1",
                     b"2026-06-15T14:03:05.000000000Z w1\n")
    with patch.object(il, "instance_is_deployed", return_value=True), \
            patch.object(il, "list_instance_containers", return_value=[web]):
        body = il.instance_logs("i1", "container", 100, None, service="nope")
    assert body["lines"] == []  # no matching container -> empty, not an error


def test_container_missing_when_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    with patch.object(il, "instance_is_deployed", return_value=False):
        assert il.instance_logs("never", "container", 100, None) is None
