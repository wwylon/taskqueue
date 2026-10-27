"""Aggregation: tq agents / GET /api/agent-usage / MCP agent_usage_summary."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue import mcp_server
from taskqueue import usage_track as ut
from taskqueue.agent_stats import agent_usage, tasks_by, usage_by
from taskqueue.cli import _cmd_agents
from taskqueue.db import open_db
from tests.helpers.master_worker import http_request, make_handler_env


def _task(db, tid, status, via=None, by=None, client=None, project="p", err=None, wall=None):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, "
        "submitted_via, submitted_by, client_info, last_error_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project, tid, "x", status, now, now, via, by, client, err),
    )
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, wall_sec) VALUES (?,?,?,?,?)",
        (project, tid, now, status, wall),
    )
    db.execute("UPDATE tasks SET last_run_id=? WHERE project=? AND task_id=?", (cur.lastrowid, project, tid))
    db.commit()


@pytest.fixture()
def seeded(tmp_path: Path):
    ut.RECORDER._acc.clear()
    db = open_db(tqconfig.db_path(tmp_path))
    for _ in range(3):
        ut.RECORDER.record("mcp", "wait_for_task", client="mcp/cursor 1", operator="ada", project="p", ms=100)
    ut.RECORDER.record("mcp", "wait_for_task", client="mcp/cursor 1", operator="ada", project="p", ms=300,
                       error_code="wait.timeout")
    ut.RECORDER.record("cli", "tq list", client="tq-cli/1", operator="bob", ms=10)
    ut.RECORDER.flush(db)
    _task(db, "a", "completed", "mcp", "ada", "mcp/cursor 1", wall=10)
    _task(db, "b", "completed", "mcp", "ada", "mcp/cursor 1", wall=30)
    _task(db, "c", "failed", "mcp", "ada", "mcp/cursor 1", err="tq.run.nonzero")
    _task(db, "d", "running", "cli", "bob", "tq-cli/1")
    _task(db, "e", "queued")  # legacy row, no provenance
    return tmp_path, db


def test_usage_group_by(seeded) -> None:
    _, db = seeded
    rows = usage_by(db, by="channel,client")
    top = rows[0]
    # wait.timeout is a normal wait outcome: visible as error_code, not counted in errors
    assert (top["channel"], top["client"], top["calls"], top["errors"]) == ("mcp", "mcp/cursor 1", 4, 0)
    codes = {r["error_code"]: r["calls"] for r in usage_by(db, by="error_code")}
    assert codes["wait.timeout"] == 1
    # wait.* durations are excluded from avg/max (notice latency), not from calls
    assert top["avg_ms"] == 100.0 and top["max_ms"] == 100
    ops = {r["op"]: r["calls"] for r in usage_by(db, by="op")}
    assert ops == {"wait_for_task": 4, "tq list": 1}
    assert usage_by(db, by="day")[0]["calls"] == 5
    assert usage_by(db, by="op", channel="cli")[0]["op"] == "tq list"
    with pytest.raises(ValueError):
        usage_by(db, by="op; DROP TABLE tasks")


def test_task_outcomes_by_provenance(seeded) -> None:
    _, db = seeded
    rows = {(r["via"], r["by"]): r for r in tasks_by(db)}
    m = rows[("mcp", "ada")]
    assert (m["tasks"], m["completed"], m["failed"]) == (3, 2, 1)
    assert m["wall_sec_p50"] == 10.0 and m["wall_sec_p95"] == 30.0
    assert m["top_errors"] == [{"code": "tq.run.nonzero", "count": 1}]
    assert rows[("cli", "bob")]["active"] == 1
    assert rows[("(unknown)", "(unknown)")]["tasks"] == 1  # legacy rows are visible, not hidden
    assert tasks_by(db, by="project")[0]["project"] == "p"


def test_http_route_mcp_tool_and_cli(seeded, monkeypatch, capsys) -> None:
    home, db = seeded
    out = agent_usage(db, days=7)
    assert out["tracking_enabled"] is True and out["usage"] and out["tasks"]
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    res = mcp_server._call_tool(home, "agent_usage_summary", {"by": "op", "days": 1})
    assert {r["op"] for r in res["usage"]} == {"wait_for_task", "tq list"}
    assert any(t["name"] == "agent_usage_summary" for t in mcp_server.TOOLS)
    assert _cmd_agents(home, ["--by", "op", "--json"]) == 0
    assert '"wait_for_task"' in capsys.readouterr().out
    assert _cmd_agents(home, ["--by", "nope"]) == 1
    HandlerClass, hdb, _ = make_handler_env(home / "h")
    code, data = http_request(HandlerClass, "GET", "/api/agent-usage?days=1&by=bogus")
    assert code == 400
    code, data = http_request(HandlerClass, "GET", "/api/agent-usage?days=1")
    assert code == 200 and "usage" in data and "tasks" in data
