"""WP4: shared queries, stop-with-control, MCP operator gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskqueue.db import open_db
from taskqueue._db_tasks import request_stop_with_control
from taskqueue.mcp_server import TOOLS, _call_tool
from taskqueue.queries import list_attention, list_operator_tasks


def _seed(conn, project: str, task_id: str, *, status: str, started_by: str | None = None) -> None:
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (project, task_id, status, command, started_by, enabled, "
        "enqueued_at, updated_at) VALUES (?, ?, ?, 'true', ?, 1, ?, ?)",
        (project, task_id, status, started_by, now, now),
    )
    conn.commit()


def test_list_attention_and_operator_tasks(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, "p", "ok", status="queued", started_by="alice")
    _seed(db, "p", "bad", status="failed", started_by="alice")
    _seed(db, "p", "other", status="failed", started_by="bob")
    _seed(db, "p", "leak", status="queued", started_by="bob")
    db.execute(
        "UPDATE tasks SET last_error_code = 'E_TEST' WHERE project='p' AND task_id='bad'"
    )
    db.execute(
        "UPDATE tasks SET last_error_code = 'E_LEAK' WHERE project='p' AND task_id='leak'"
    )
    db.commit()

    attn = list_attention(db, operator="alice")
    assert [a["task_id"] for a in attn] == ["bad"]
    assert attn[0]["error_description"]
    # Bob's errored queued task must not leak into alice's filter (SQL precedence).
    assert all(a["started_by"] == "alice" for a in attn)

    mine = list_operator_tasks(db, operator="alice")
    assert {r["task_id"] for r in mine} == {"ok", "bad"}
    db.close()


def test_request_stop_with_control(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    _seed(db, "p", "t1", status="running", started_by="alice")
    path = request_stop_with_control(db, home, "p", "t1")
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["action"] == "suspend"
    row = db.execute(
        "SELECT stop_requested FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()
    assert row["stop_requested"] == 1
    db.close()


def test_mcp_my_tasks_requires_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    open_db(home / "queue.db").close()
    monkeypatch.delenv("TQ_OPERATOR", raising=False)
    with pytest.raises(ValueError, match="operator required"):
        _call_tool(home, "my_tasks", {})


def test_mcp_tools_collapsed_aliases() -> None:
    names = {t["name"] for t in TOOLS}
    assert "attention_list" in names
    assert "task_health" in names
    assert "attention_for_operator" not in names
    assert "watch_attention" not in names
    assert "my_tasks" in names
    my = next(t for t in TOOLS if t["name"] == "my_tasks")
    assert "required" not in my.get("inputSchema", {})
