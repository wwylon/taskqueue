"""Wave-2 scale guards: heavy queue sorts, deps early filter, paginated runs, task-stats cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.operator_features import TASK_STATS_UNSCOPED_LIMIT, task_stats
from taskqueue.web import _get_deps_response
from taskqueue.web.responses import HEAVY_SORT_TOTAL_THRESHOLD, _get_queue_response
from tests.helpers.master_worker import http_request, make_handler_env


def _task(conn, project: str, task_id: str, *, status: str = "queued", suite: str = "") -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, suite, depends_on, depends_on_any, "
        "enqueued_at, updated_at) VALUES (?, ?, 'x', ?, ?, '[]', '[]', ?, ?)",
        (project, task_id, status, suite, now, now),
    )


def _run(conn, project: str, task_id: str, *, started: str, finished: str | None = None, status: str = "completed") -> None:
    conn.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status) VALUES (?,?,?,?,?)",
        (project, task_id, started, finished or started, status),
    )


def test_heavy_sort_requires_project_when_total_large(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    n = HEAVY_SORT_TOTAL_THRESHOLD + 3
    for i in range(n):
        _task(conn, "p", f"t{i}")
    conn.commit()

    blocked = _get_queue_response(conn, sort="deps", limit=10, offset=0)
    assert blocked["error"] == "project_required_for_sort"
    assert blocked["tasks"] == []
    assert blocked["total"] == n

    blocked_order = _get_queue_response(conn, sort="order", limit=10, offset=0)
    assert blocked_order["error"] == "project_required_for_sort"

    ok = _get_queue_response(conn, project="p", sort="deps", limit=10, offset=0)
    assert "error" not in ok
    assert len(ok["tasks"]) == 10
    conn.close()


def test_deps_suite_and_focus_filter_early(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    _task(conn, "alpha", "a", status="completed", suite="loc")
    _task(conn, "alpha", "b", status="queued", suite="loc")
    conn.execute(
        "UPDATE tasks SET depends_on = ? WHERE project = ? AND task_id = ?",
        ('["a"]', "alpha", "b"),
    )
    _task(conn, "alpha", "c", status="queued", suite="other")
    _task(conn, "beta", "x", status="queued", suite="loc")
    conn.commit()

    suite_only = _get_deps_response(conn, suite="loc")
    ids = {n["id"] for n in suite_only["nodes"]}
    assert "alpha/a" in ids
    assert "alpha/b" in ids
    assert "beta/x" in ids
    assert "alpha/c" not in ids

    focused = _get_deps_response(conn, focus="alpha/b")
    fids = {n["id"] for n in focused["nodes"]}
    assert "alpha/b" in fids
    assert "alpha/a" in fids
    assert "beta/x" not in fids
    conn.close()


def test_task_runs_api_paginates(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    _task(db, "p", "t", status="completed")
    now = datetime.now(UTC)
    for i in range(12):
        _run(
            db,
            "p",
            "t",
            started=(now - timedelta(minutes=12 - i)).isoformat(),
            finished=(now - timedelta(minutes=11 - i)).isoformat(),
        )
    db.commit()

    code, body = http_request(HandlerClass, "GET", "/api/tasks/p/t/runs?limit=5&offset=0")
    assert code == 200
    assert body["total"] == 12
    assert body["limit"] == 5
    assert len(body["runs"]) == 5
    page2 = http_request(HandlerClass, "GET", "/api/tasks/p/t/runs?limit=5&offset=5")[1]
    assert len(page2["runs"]) == 5
    assert {r["run_id"] for r in body["runs"]}.isdisjoint({r["run_id"] for r in page2["runs"]})


def test_task_stats_unscoped_cap(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    now = datetime.now(UTC)
    n = TASK_STATS_UNSCOPED_LIMIT + 5
    for i in range(n):
        _task(conn, "p", f"t{i}", status="completed")
        _run(
            conn,
            "p",
            f"t{i}",
            started=(now - timedelta(hours=1)).isoformat(),
            finished=now.isoformat(),
        )
    conn.commit()
    out = task_stats(conn)
    assert len(out) == TASK_STATS_UNSCOPED_LIMIT
    scoped = task_stats(conn, project="p")
    assert len(scoped) == n
    conn.close()
