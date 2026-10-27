"""W2.2: Performance regression suite — budget assertions at target profile.

Run after loadgen --profile target to set a baseline; subsequent runs must
stay within budget. Tests are marked slow and skipped unless LOADGEN_SEEDED=1.

Budgets (from plan W2.2):
    /api/queue p95   < 200 ms
    /api/status p95  < 100 ms
    DB growth        < 5 MB per 1k tasks
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

# Skip unless explicitly opted in (loadgen must be run first).
_SEEDED = os.environ.get("LOADGEN_SEEDED", "0") == "1"
skip_unless_seeded = pytest.mark.skipif(not _SEEDED, reason="set LOADGEN_SEEDED=1 after loadgen --profile target")


# ---------------------------------------------------------------------------
# Index budget: verify EXPLAIN QUERY PLAN uses index on /api/queue lookup.
# ---------------------------------------------------------------------------

def test_runs_query_uses_index(tmp_path):
    """The runs lookup for /api/queue must use an index, not a full scan."""
    from taskqueue.db import open_db
    db = open_db(tmp_path / "q.db")

    plan = db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC",
        ("proj", "task_001"),
    ).fetchall()
    # EXPLAIN QUERY PLAN rows: (id, parent, notused, detail)
    plan_text = " ".join(row[3] if len(row) > 3 else str(tuple(row)) for row in plan).upper()
    assert "USING INDEX" in plan_text or "SEARCH" in plan_text, (
        f"Expected index use, got: {plan_text!r}"
    )


def test_tasks_priority_query_uses_index(tmp_path):
    """Scheduler ORDER BY priority DESC, enqueued_at ASC should use the covering index."""
    from taskqueue.db import open_db
    db = open_db(tmp_path / "q.db")

    plan = db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM tasks WHERE status='queued' ORDER BY priority DESC, enqueued_at ASC LIMIT 50"
    ).fetchall()
    plan_text = " ".join(row[3] if len(row) > 3 else str(tuple(row)) for row in plan).upper()
    # Pass if it uses any index; only fail on a confirmed unindexed full scan.
    has_scan = "SCAN TASKS" in plan_text and "USING INDEX" not in plan_text
    assert not has_scan, f"Full scan detected on tasks scheduling query: {plan_text!r}"


# ---------------------------------------------------------------------------
# DB size budget: < 5 MB per 1k tasks
# ---------------------------------------------------------------------------

def test_db_size_budget(tmp_path):
    """1000 tasks + 1000 runs should produce < 5 MB of DB."""
    from taskqueue.db import open_db
    import json

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"

    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) VALUES (?,?,?,?,?,?)",
        [(f"p{i % 10}", f"t{i:04d}", "python", "queued", now, now) for i in range(1000)],
    )
    db.executemany(
        "INSERT INTO runs (project, task_id, started_at, status) VALUES (?,?,?,?)",
        [(f"p{i % 10}", f"t{i:04d}", now, "completed") for i in range(1000)],
    )
    db.commit()

    size_mb = (tmp_path / "q.db").stat().st_size / (1024 * 1024)
    assert size_mb < 5.0, f"DB too large: {size_mb:.2f} MB for 1k tasks/runs (budget: 5 MB)"


# ---------------------------------------------------------------------------
# W4.1: server-side pagination and search correctness
# ---------------------------------------------------------------------------

def test_queue_api_paginated(tmp_path):
    """500 tasks — /api/queue response honours limit=10 and total is correct."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_queue_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, priority) "
        "VALUES (?,?,?,?,?,?,?)",
        [(f"p{i % 5}", f"t{i:04d}", "python", "queued", now, now, 50) for i in range(500)],
    )
    db.commit()

    result = _get_queue_response(db, limit=10, offset=0)
    assert len(result["tasks"]) == 10
    assert result["total"] == 500
    assert result["limit"] == 10
    assert result["offset"] == 0

    result2 = _get_queue_response(db, limit=10, offset=10)
    # tasks should be different pages
    ids1 = {t["task_id"] for t in result["tasks"]}
    ids2 = {t["task_id"] for t in result2["tasks"]}
    assert ids1.isdisjoint(ids2), "Pagination returned overlapping task IDs"


def test_search_is_server_side(tmp_path):
    """?search= narrows the SQL result; client receives only matches."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_queue_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, priority, description) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [("proj", f"needle_{i}", "python", "queued", now, now, 50, "unique-needle") for i in range(5)]
        + [("proj", f"haystack_{i}", "python", "queued", now, now, 50, "irrelevant") for i in range(45)],
    )
    db.commit()

    result = _get_queue_response(db, search="needle", limit=100, offset=0)
    assert result["total"] == 5
    assert all("needle" in t["task_id"] for t in result["tasks"])


def test_search_by_description(tmp_path):
    """?search= also matches on description field."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_queue_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, priority, description) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [("proj", f"any_{i}", "python", "queued", now, now, 50, "train_model_v2") for i in range(3)]
        + [("proj", f"other_{i}", "python", "queued", now, now, 50, "eval_run") for i in range(7)],
    )
    db.commit()

    result = _get_queue_response(db, search="train_model", limit=100, offset=0)
    assert result["total"] == 3


# ---------------------------------------------------------------------------
# W5.3: _get_history_response extracted for testability
# ---------------------------------------------------------------------------

def test_groups_response(tmp_path):
    """_get_groups_response: returns status breakdown per group."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_groups_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, grp) VALUES (?,?,?,?,?,?,?)",
        [("p", f"a{i}", "python", "queued", now, now, "grpA") for i in range(3)]
        + [("p", f"b{i}", "python", "running", now, now, "grpB") for i in range(2)],
    )
    db.commit()

    r = _get_groups_response(db)
    names = {g["name"] for g in r["groups"]}
    assert "grpA" in names and "grpB" in names
    grpA = next(g for g in r["groups"] if g["name"] == "grpA")
    assert grpA["total"] == 3
    assert grpA["by_status"]["queued"] == 3
    assert grpA["projects"] == ["p"]


def test_cost_response(tmp_path):
    """_get_cost_response: aggregates CPU-minutes from runs."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_cost_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) VALUES (?,?,?,?,?,?)",
        [("alpha", "t1", "python", "completed", now, now)],
    )
    db.executemany(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at) VALUES (?,?,?,?,?)",
        [("alpha", "t1", "completed", "2026-01-01T00:00:00+00:00", "2026-01-01T00:02:00+00:00")],  # 2 min
    )
    db.commit()

    r = _get_cost_response(db)
    assert "alpha" in r["by_project"]
    assert r["by_project"]["alpha"]["cpu_minutes"] == pytest.approx(2.0, abs=0.1)
    assert r["total_tasks"] == 1


def test_deps_response_nodes_and_edges(tmp_path):
    """_get_deps_response: nodes include all tasks, edges capture depends_on."""
    import json as _json
    from taskqueue.db import open_db
    from taskqueue.web import _get_deps_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, depends_on) "
        "VALUES (?,?,?,?,?,?,?)",
        ("proj", "upstream", "python", "completed", now, now, "[]"),
    )
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, depends_on) "
        "VALUES (?,?,?,?,?,?,?)",
        ("proj", "downstream", "python", "queued", now, now, '["upstream"]'),
    )
    db.commit()

    r = _get_deps_response(db, project="proj")
    node_ids = {n["id"] for n in r["nodes"]}
    assert "upstream" in node_ids and "downstream" in node_ids
    assert any(e["from"] == "upstream" and e["to"] == "downstream" for e in r["edges"])


def test_usage_response_empty(tmp_path):
    """_get_usage_response: no crash on empty usage_snapshot table."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_usage_response

    db = open_db(tmp_path / "q.db")
    r = _get_usage_response(db, cutoff_ts=0)
    assert r["by_project"] == {}
    assert r["projects"] == []


def test_history_api_paginated(tmp_path):
    """_get_history_response: total=N, pagination returns non-overlapping pages."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_history_response

    db = open_db(tmp_path / "q.db")
    now_start = "2026-01-01T00:00:00+00:00"
    now_end = "2026-01-01T00:05:00+00:00"
    db.executemany(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at) VALUES (?,?,?,?,?)",
        [(f"p{i % 3}", f"t{i:04d}", "completed", now_start, now_end) for i in range(60)],
    )
    db.commit()

    r1 = _get_history_response(db, limit=10, offset=0)
    assert r1["total"] == 60
    assert len(r1["runs"]) == 10

    r2 = _get_history_response(db, limit=10, offset=10)
    ids1 = {r["task_id"] for r in r1["runs"]}
    ids2 = {r["task_id"] for r in r2["runs"]}
    assert ids1.isdisjoint(ids2), "Pages should not overlap"


def test_history_api_project_filter(tmp_path):
    """_get_history_response: project filter narrows total and runs."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_history_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO runs (project, task_id, status, started_at) VALUES (?,?,?,?)",
        [("alpha", f"a{i}", "completed", now) for i in range(5)]
        + [("beta", f"b{i}", "completed", now) for i in range(8)],
    )
    db.commit()

    r = _get_history_response(db, project="alpha", limit=100, offset=0)
    assert r["total"] == 5
    assert all(run["project"] == "alpha" for run in r["runs"])


def test_history_api_search(tmp_path):
    """_get_history_response: ?search= matches task_id / project / #run_id before LIMIT."""
    from taskqueue.db import open_db
    from taskqueue.web import _get_history_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO runs (project, task_id, status, started_at, error_category, worker_id) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("alpha", "train_needle", "completed", now, None, None),
            ("alpha", "other", "failed", now, "oom_kill", None),
            ("beta", "noise_0", "completed", now, None, "worker-b"),
            ("beta", "noise_1", "completed", now, None, None),
        ],
    )
    db.commit()

    by_task = _get_history_response(db, search="needle", limit=100, offset=0)
    assert by_task["total"] == 1
    assert by_task["runs"][0]["task_id"] == "train_needle"

    by_err = _get_history_response(db, search="oom", limit=100, offset=0)
    assert by_err["total"] == 1
    assert by_err["runs"][0]["error_category"] == "oom_kill"

    by_worker = _get_history_response(db, search="worker-b", limit=100, offset=0)
    assert by_worker["total"] == 1
    assert by_worker["runs"][0]["worker_id"] == "worker-b"

    run_id = by_task["runs"][0]["run_id"]
    by_hash = _get_history_response(db, search=f"#{run_id}", limit=100, offset=0)
    assert by_hash["total"] == 1
    assert by_hash["runs"][0]["run_id"] == run_id

    scoped = _get_history_response(db, project="beta", search="noise", limit=100, offset=0)
    assert scoped["total"] == 2
    assert all(r["project"] == "beta" for r in scoped["runs"])


# ---------------------------------------------------------------------------
# /api/queue response time budget (requires seeded data)
# ---------------------------------------------------------------------------

@skip_unless_seeded
def test_api_queue_p95(tmp_path):
    """Measure live `_get_queue_response` against the target-profile DB."""
    home = Path(os.environ.get("TASKQUEUE_TEST_HOME", str(Path.home() / ".taskqueue-dev")))
    db_path = home / "queue.db"
    if not db_path.exists():
        pytest.skip("Run loadgen --profile target first")

    from taskqueue.db import open_db
    from taskqueue.web.responses import _get_queue_response

    conn = open_db(db_path, migrate=False)
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        _get_queue_response(conn, limit=200, offset=0)
        times.append((time.perf_counter() - t0) * 1000)

    times.sort()
    p95 = times[int(len(times) * 0.95)]
    print(f"\n/api/queue p95: {p95:.1f} ms (budget: 200 ms)")
    assert p95 < 200, f"/api/queue p95 {p95:.1f} ms exceeds 200 ms budget"


@skip_unless_seeded
def test_api_status_p95(tmp_path):
    """Measure status_counts + running/queued counts against the target-profile DB."""
    home = Path(os.environ.get("TASKQUEUE_TEST_HOME", str(Path.home() / ".taskqueue-dev")))
    db_path = home / "queue.db"
    if not db_path.exists():
        pytest.skip("Run loadgen --profile target first")

    from taskqueue.db import open_db
    from taskqueue.queries import status_counts
    from taskqueue._db_tasks import count_queued_tasks, get_running_tasks

    conn = open_db(db_path, migrate=False)
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        status_counts(conn)
        get_running_tasks(conn)
        count_queued_tasks(conn)
        conn.execute("SELECT * FROM workers").fetchall()
        times.append((time.perf_counter() - t0) * 1000)

    times.sort()
    p95 = times[int(len(times) * 0.95)]
    print(f"\n/api/status p95: {p95:.1f} ms (budget: 100 ms)")
    assert p95 < 100, f"/api/status p95 {p95:.1f} ms exceeds 100 ms budget"


def test_count_queued_tasks_matches_fetch(tmp_path):
    from taskqueue.db import open_db
    from taskqueue._db_tasks import count_queued_tasks, get_queued_tasks

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("p", "q1", "python", "queued", now, now, 1),
            ("p", "q2", "python", "stopped", now, now, 1),
            ("p", "d1", "python", "disabled", now, now, 0),
            ("p", "r1", "python", "running", now, now, 1),
        ],
    )
    db.commit()
    assert count_queued_tasks(db) == len(get_queued_tasks(db)) == 2


def test_slim_dto_projections():
    from taskqueue.web import _history_row_dict, _queue_row_dict

    fat_task = {
        "project": "p",
        "task_id": "t1",
        "status": "queued",
        "command": "python -m train",
        "metadata": {"x": 1},
        "priority": 50,
    }
    slim = _queue_row_dict(fat_task)
    assert "command" not in slim
    assert "metadata" not in slim
    assert slim["project"] == "p"

    run = {
        "run_id": 1,
        "project": "p",
        "task_id": "t1",
        "status": "completed",
        "metrics_json": '{"log": {}}',
        "log_path": "/tmp/log",
        "pid": 1234,
    }
    slim_run = _history_row_dict(run)
    assert "metrics_json" not in slim_run
    assert "log_path" not in slim_run
    assert slim_run["run_id"] == 1
