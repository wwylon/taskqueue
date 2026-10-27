"""Terminal events always written; operator matches submitter; cursor + long-poll + piggyback."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue import mcp_server
from taskqueue.db import open_db
from taskqueue._db_tasks import stamp_provenance
from taskqueue.notify import (
    append_notify_event,
    drain_notifications,
    emit_task_notify,
    notify_log_path,
)

CFG = {"toast_notifications": False}


def _emit(home: Path, task_id: str = "t1", status: str = "completed", **kw) -> dict | None:
    return emit_task_notify(
        home, project="p", task_id=task_id, status=status, run_id=1, exit_code=0,
        last_error_code=None, notify_url=None, started_by=kw.pop("started_by", None),
        cfg=kw.pop("cfg", CFG), **kw,
    )


def _seed_task(home: Path, task_id: str, via: str, by: str) -> None:
    db = open_db(tqconfig.db_path(home))
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES ('p', ?, 'x', 'completed', 'n', 'n')", (task_id,))
    stamp_provenance(db, "p", [task_id], via, by, "mcp/cursor 1")
    db.close()


def test_terminal_event_written_without_notify_url_with_provenance(tmp_path: Path) -> None:
    _seed_task(tmp_path, "t1", "mcp", "ada")
    payload = _emit(tmp_path)
    assert payload and payload["submitted_by"] == "ada" and payload["submitted_via"] == "mcp"
    lines = notify_log_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["task_id"] == "t1"
    assert _emit(tmp_path, status="running") is None  # non-terminal stays out


def test_notify_default_can_be_disabled(tmp_path: Path) -> None:
    assert _emit(tmp_path, cfg={**CFG, "notify_default": 0}) is None
    assert _emit(tmp_path, cfg={**CFG, "notify_default": "false"}) is None
    assert not notify_log_path(tmp_path).exists()
    assert "notify_default" in tqconfig.VALID_CONFIG_KEYS


def test_operator_filter_matches_submitter_or_starter(tmp_path: Path) -> None:
    _seed_task(tmp_path, "t1", "mcp", "ada")
    _emit(tmp_path, "t1")                       # submitted_by ada, started_by None
    _emit(tmp_path, "t2", started_by="bob")     # started_by bob
    ada = drain_notifications(tmp_path, operator="ada")["events"]
    bob = drain_notifications(tmp_path, operator="bob")["events"]
    assert [e["task_id"] for e in ada] == ["t1"] and [e["task_id"] for e in bob] == ["t2"]


def test_incremental_cursor_does_not_drop_first_new_event(tmp_path: Path) -> None:
    def ev(i: int) -> dict:
        return {"ts": f"2026-01-01T00:00:0{i}+00:00", "project": "p", "task_id": f"t{i}", "status": "completed",
                "started_by": "ada"}

    append_notify_event(tmp_path, ev(1))
    first = drain_notifications(tmp_path, operator="ada", incremental=True)
    assert [e["task_id"] for e in first["events"]] == ["t1"]
    append_notify_event(tmp_path, ev(2))
    second = drain_notifications(tmp_path, operator="ada", incremental=True)
    assert [e["task_id"] for e in second["events"]] == ["t2"]  # used to be silently lost
    assert drain_notifications(tmp_path, operator="ada", incremental=True)["count"] == 0
    # half-written trailing line is left for the next call
    path = notify_log_path(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev(3))[:20])
    assert drain_notifications(tmp_path, operator="ada", incremental=True)["count"] == 0
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev(3))[20:] + "\n")
    third = drain_notifications(tmp_path, operator="ada", incremental=True)
    assert [e["task_id"] for e in third["events"]] == ["t3"]


def test_drain_wait_sec_long_polls(tmp_path: Path) -> None:
    t0 = time.monotonic()
    assert drain_notifications(tmp_path, wait_sec=0.3)["count"] == 0
    assert 0.25 <= time.monotonic() - t0 < 3

    def later() -> None:
        time.sleep(0.4)
        _emit(tmp_path, "late")

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    out = drain_notifications(tmp_path, wait_sec=5)
    assert [e["task_id"] for e in out["events"]] == ["late"]
    assert time.monotonic() - t0 < 4


def test_mcp_piggybacks_finished_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    monkeypatch.setenv("TQ_OPERATOR", "ada")
    open_db(tqconfig.db_path(tmp_path)).close()
    monkeypatch.setitem(mcp_server._PIGGY, "ts", None)
    monkeypatch.setitem(mcp_server._PIGGY, "size", -1)
    assert "finished_since_last_call" not in mcp_server._call_tool(tmp_path, "queue_status", {})
    assert mcp_server._finished_since_last_call(tmp_path) == []  # baseline taken, nothing new
    time.sleep(0.01)
    _emit(tmp_path, "done1", started_by="ada")
    _emit(tmp_path, "other", started_by="bob")
    got = mcp_server._finished_since_last_call(tmp_path)
    assert [e["task_id"] for e in got] == ["done1"]
    assert mcp_server._finished_since_last_call(tmp_path) == []  # consumed
    monkeypatch.delenv("TQ_OPERATOR")
    _emit(tmp_path, "done2", started_by="ada")
    assert mcp_server._finished_since_last_call(tmp_path) == []  # no operator, no piggyback


def test_unknown_status_is_a_terminal_event(tmp_path: Path) -> None:
    """`unknown` (lost worker) settles `tq wait`; it must also reach notify.jsonl."""
    payload = _emit(tmp_path, "lost", status="unknown")
    assert payload is not None and payload["status"] == "unknown"
    assert [e["task_id"] for e in drain_notifications(tmp_path)["events"]] == ["lost"]
