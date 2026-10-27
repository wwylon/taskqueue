"""Shared wait logic: tq wait / MCP wait_for_task (docs/mcp.md, docs/errors.md)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue import mcp_server
from taskqueue._cli_task import cmd_wait
from taskqueue._db_tasks import _TERMINAL_STATUSES
from taskqueue.db import open_db
from taskqueue.waiting import (
    SETTLED_STATUSES,
    TERMINAL_STATUSES,
    exit_code_for,
    resolve_selectors,
    wait_for,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _mk(conn, project: str, task_id: str, status: str = "queued", **cols) -> None:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, suite, grp, tags, started_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project, task_id, "python", status, _now(), _now(), cols.get("suite", ""),
         cols.get("grp", ""), json.dumps(cols.get("tags", [])), cols.get("started_by")),
    )
    conn.commit()


def _run(conn, project: str, task_id: str, *, exit_code: int, status: str) -> None:
    cur = conn.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, exit_code, wall_sec, last_log_line) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (project, task_id, _now(), _now(), status, exit_code, 7, "last line"),
    )
    conn.execute(
        "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?",
        (cur.lastrowid, project, task_id),
    )
    conn.commit()


def _set(db_path: Path, project: str, task_id: str, status: str) -> None:
    c = open_db(db_path)
    c.execute("UPDATE tasks SET status = ? WHERE project = ? AND task_id = ?", (status, project, task_id))
    c.commit()
    c.close()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    open_db(tqconfig.db_path(tmp_path)).close()
    return tmp_path


def _opener(home: Path):
    return lambda: open_db(tqconfig.db_path(home), migrate=False)


def test_single_terminal_definition() -> None:
    assert TERMINAL_STATUSES is _TERMINAL_STATUSES
    assert "suspended" in TERMINAL_STATUSES and "unknown" in TERMINAL_STATUSES
    assert mcp_server._TERMINAL_STATUSES == SETTLED_STATUSES


def test_wait_all_returns_when_every_task_settles(home: Path) -> None:
    db = tqconfig.db_path(home)
    c = open_db(db)
    _mk(c, "p", "a", "running")
    _mk(c, "p", "b", "completed")
    c.close()
    ticks = {"n": 0}

    def fake_sleep(_d: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 2:
            _set(db, "p", "a", "completed")

    res = wait_for(_opener(home), ["p/a", "p/b"], sleep=fake_sleep, timeout_sec=1000)
    assert res["done"] and not res["timed_out"]
    assert ticks["n"] == 2
    assert exit_code_for(res) == 0


def test_wait_any_returns_on_first(home: Path) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "a", "running")
    _mk(c, "p", "b", "failed")
    c.close()
    res = wait_for(_opener(home), ["p/a", "p/b"], mode="any", sleep=lambda d: None)
    assert res["done"] and res["pending"] == ["p/a"]
    assert exit_code_for(res) == 1  # settled task failed


def test_timeout_and_suspended_is_terminal(home: Path) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "a", "running")
    _mk(c, "p", "s", "suspended")
    c.close()
    t = {"now": 0.0}

    def clock() -> float:
        return t["now"]

    def sleep(d: float) -> None:
        t["now"] += d

    res = wait_for(_opener(home), ["p/a"], timeout_sec=3, sleep=sleep, clock=clock)
    assert res["timed_out"] and not res["done"] and exit_code_for(res) == 2
    assert res["hint"]
    # suspended no longer hangs the wait forever
    res2 = wait_for(_opener(home), ["p/s"], sleep=lambda d: None)
    assert res2["done"] and res2["tasks"][0]["finished"]


def test_not_found_and_disabled(home: Path) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "d", "disabled")
    c.close()
    res = wait_for(_opener(home), ["p/nope"], sleep=lambda d: None)
    assert res["not_found"] == ["p/nope"] and exit_code_for(res) == 3
    res = wait_for(_opener(home), ["p/d"], sleep=lambda d: None)
    assert res["done"] and not res["tasks"][0]["finished"]  # settled but not finished


def test_selectors_only_pick_unsettled(home: Path) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "old", "completed", suite="s1")
    _mk(c, "p", "new", "running", suite="s1", tags=["x", "y"])
    _mk(c, "p", "other", "queued", suite="s2", tags=["x"])
    assert resolve_selectors(c, suite="s1") == ["p/new"]
    assert resolve_selectors(c, suite="s1", include_finished=True) == ["p/new", "p/old"] or \
        set(resolve_selectors(c, suite="s1", include_finished=True)) == {"p/new", "p/old"}
    assert resolve_selectors(c, tags=["x", "y"]) == ["p/new"]
    assert resolve_selectors(c, keys=["p/old"], suite="s2") == ["p/old", "p/other"]
    with pytest.raises(ValueError):
        resolve_selectors(c, keys=["bad"])
    c.close()


def test_cli_exit_codes_and_raw_exit(home: Path, capsys) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "ok", "completed")
    _run(c, "p", "ok", exit_code=0, status="completed")
    _mk(c, "p", "bad", "failed")
    _run(c, "p", "bad", exit_code=7, status="failed")
    _mk(c, "p", "run", "running")
    c.close()
    assert cmd_wait(home, ["p/ok"]) == 0
    assert cmd_wait(home, ["p/ok", "p/bad"]) == 1
    assert cmd_wait(home, ["p/nope"]) == 3
    assert cmd_wait(home, ["p/run"], timeout_sec=0.05, poll_sec=0.05) == 2
    assert cmd_wait(home, ["p/bad"], raw_exit=True) == 7
    assert cmd_wait(home, ["p/run"], raw_exit=True, timeout_sec=0.05, poll_sec=0.05) == 124
    capsys.readouterr()
    assert cmd_wait(home, ["p/bad"], as_json=True) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["tasks"][0]["exit_code"] == 7 and out["tasks"][0]["wall_sec"] == 7


def test_mcp_wait_and_result(home: Path) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "bad", "failed")
    _run(c, "p", "bad", exit_code=3, status="failed")
    _mk(c, "p", "run", "running")
    c.close()
    res = mcp_server._wait_for_task(home, {"project": "p", "task_id": "bad"})
    assert res["done"] and res["status"] == "failed" and res["exit_code"] == 3
    assert res["tasks"][0]["next_actions"] and "log_tail" in res["tasks"][0]
    # timeout is clamped to the hard cap and reports timed_out
    res = mcp_server._wait_for_task(home, {"tasks": ["p/run"], "timeout_sec": 0.2, "poll_sec": 0.1})
    assert res["timed_out"] and res["pending"] == ["p/run"]
    r = mcp_server._task_result(home, {"project": "p", "task_id": "bad"})
    assert r["ok"] and r["exit_code"] == 3 and r["last_lines"] == ["last line"]
    with pytest.raises(ValueError):
        mcp_server._wait_for_task(home, {})
    assert mcp_server._mcp_wait_max_sec() == 100.0


def test_mcp_stdio_wait_runs_off_thread(home: Path, capsys) -> None:
    c = open_db(tqconfig.db_path(home))
    _mk(c, "p", "done", "completed")
    c.close()
    mcp_server._run_tool_call(
        home, 5, {"name": "wait_for_task", "arguments": {"project": "p", "task_id": "done"}}
    )
    msg = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert msg["id"] == 5 and msg["result"]["structuredContent"]["done"] is True
