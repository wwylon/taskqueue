from __future__ import annotations

import json
from pathlib import Path

from taskqueue.cli import _cmd_explain
from taskqueue.db import open_db
from taskqueue.mcp_server import _call_tool


def _seed(
    conn,
    task_id: str,
    *,
    status: str = "failed",
    suite: str = "",
    grp: str = "",
    depends_on: list[str] | None = None,
    enabled: int = 1,
) -> None:
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, enabled, priority, "
        "resources, depends_on, exclusive, max_attempts, suite, grp, enqueued_at, updated_at) "
        "VALUES ('p', ?, ?, 'echo', '[]', ?, 50, '{}', ?, 0, 0, ?, ?, ?, ?)",
        (task_id, status, enabled, json.dumps(depends_on or []), suite, grp, now, now),
    )
    conn.commit()


def test_tq_explain_catalog_and_unknown(capsys) -> None:
    assert _cmd_explain(Path("."), ["tq.block.deps"]) == 0
    assert "Dependency tasks not completed successfully" in capsys.readouterr().out
    assert _cmd_explain(Path("."), ["not-a-code"]) == 1
    assert "unknown code" in capsys.readouterr().err


def test_mcp_explain_error_is_read_only(tmp_path: Path) -> None:
    result = _call_tool(tmp_path, "explain_error", {"error_code": "tq.run.cap"})
    assert result["ok"] is True
    assert result["description"] == "Stopped after wall_clock_cap_sec"
    assert not (tmp_path / "queue.db").exists()


def test_mcp_retry_tasks_dry_run_suite_and_downstream(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    _seed(db, "root", suite="nightly")
    _seed(db, "child", suite="nightly", depends_on=["root"])
    _seed(db, "done", status="completed", suite="nightly")
    db.close()

    suite = _call_tool(tmp_path, "retry_tasks", {"suite": "nightly", "dry_run": True})
    assert suite["dry_run"] is True
    assert {a["key"] for a in suite["actions"]} == {"p/root", "p/child", "p/done"}
    assert all(a["action"] == "retry" for a in suite["actions"])
    conn = open_db(tmp_path / "queue.db")
    assert conn.execute("SELECT status FROM tasks WHERE task_id='root'").fetchone()[0] == "failed"
    conn.close()

    downstream = _call_tool(tmp_path, "retry_tasks", {"downstream_of": "p/root", "dry_run": True})
    assert [a["key"] for a in downstream["actions"]] == ["p/root", "p/child"]
