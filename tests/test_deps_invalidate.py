"""Dependency invalidation while a dependent is still running (tq.stop.deps)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from taskqueue import errors as tqerr
from taskqueue._db_tasks import (
    collect_running_dependents,
    invalidate_running_dependents,
    revalidate_running_dependencies,
    retry_task,
    unmet_dependency_label,
)
from taskqueue.db import open_db


def _open(tmp_path: Path) -> sqlite3.Connection:
    home = tmp_path / "home"
    home.mkdir()
    return open_db(home / "queue.db")


def _insert(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    status: str,
    depends_on=None,
) -> None:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, "
        "depends_on, enabled) VALUES (?,?,?,?,datetime('now'),datetime('now'),?,1)",
        (project, task_id, "python", status, json.dumps(depends_on or [])),
    )
    conn.commit()


def _insert_running_run(conn: sqlite3.Connection, project: str, task_id: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (project, task_id, status, started_at) "
        "VALUES (?,?, 'running', datetime('now'))",
        (project, task_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_unmet_dependency_label(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    _insert(conn, "p", "up", status="queued")
    _insert(conn, "p", "down", status="running", depends_on=["up"])
    assert unmet_dependency_label(conn, "p", "down") == "up [queued]"
    conn.execute("UPDATE tasks SET status='completed' WHERE task_id='up'")
    conn.commit()
    assert unmet_dependency_label(conn, "p", "down") is None
    conn.close()


def test_retry_upstream_stops_running_dependent(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    _insert(conn, "p", "c23", status="completed")
    _insert(conn, "p", "c24", status="running", depends_on=["c23"])
    run_id = _insert_running_run(conn, "p", "c24")

    assert retry_task(conn, "p", "c23") is True

    c23 = conn.execute("SELECT status FROM tasks WHERE task_id='c23'").fetchone()
    c24 = conn.execute(
        "SELECT status, stop_requested, last_error_code, block_reason FROM tasks "
        "WHERE task_id='c24'"
    ).fetchone()
    run = conn.execute("SELECT stop_reason FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert c23["status"] == "queued"
    assert c24["status"] == "running"
    assert int(c24["stop_requested"]) == 1
    assert c24["last_error_code"] == tqerr.STOP_DEPS
    assert "c23" in (c24["block_reason"] or "")
    assert run["stop_reason"] == "deps_invalidated"
    conn.close()


def test_collect_running_dependents_cross_project(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    _insert(conn, "a", "up", status="completed")
    _insert(conn, "b", "down", status="running", depends_on=["a/up"])
    _insert(conn, "b", "other", status="running", depends_on=["noop"])
    assert collect_running_dependents(conn, "a", "up") == [("b", "down")]
    conn.close()


def test_revalidate_running_dependencies(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    _insert(conn, "p", "up", status="queued")
    _insert(conn, "p", "down", status="running", depends_on=["up"])
    _insert_running_run(conn, "p", "down")

    hit = revalidate_running_dependencies(conn)
    assert hit == [("p", "down", "up [queued]")]
    row = conn.execute(
        "SELECT stop_requested, last_error_code FROM tasks WHERE task_id='down'"
    ).fetchone()
    assert int(row["stop_requested"]) == 1
    assert row["last_error_code"] == tqerr.STOP_DEPS

    # Idempotent — already stopping
    assert revalidate_running_dependencies(conn) == []
    conn.close()


def test_invalidate_running_dependents_helper(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    _insert(conn, "p", "root", status="completed")
    _insert(conn, "p", "leaf", status="running", depends_on=["root"])
    _insert_running_run(conn, "p", "leaf")
    stopped = invalidate_running_dependents(conn, "p", "root")
    assert stopped == [("p", "leaf")]
    conn.close()
