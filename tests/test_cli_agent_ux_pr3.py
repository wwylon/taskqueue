"""PR3: reverify DAG keep-set and lock occupancy."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from taskqueue._db_tasks import (
    collect_ancestors_to_root,
    collect_downstream_task_ids,
    lock_contention_summary,
    lock_occupancy,
)
from taskqueue.db import open_db, set_project_limit


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seed(
    conn,
    *,
    task_id: str,
    status: str = "completed",
    enabled: int = 1,
    depends_on: list | None = None,
    resources: dict | None = None,
    last_error_code: str | None = None,
) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, enabled, priority, "
        "resources, depends_on, exclusive, max_attempts, enqueued_at, updated_at, "
        "last_error_code) "
        "VALUES ('p', ?, ?, 'echo', '[]', ?, 50, ?, ?, 0, 0, ?, ?, ?)",
        (
            task_id,
            status,
            enabled,
            json.dumps(resources or {}),
            json.dumps(depends_on or []),
            now,
            now,
            last_error_code,
        ),
    )
    if status == "running":
        conn.execute(
            "INSERT INTO runs (project, task_id, started_at, status, log_path) "
            "VALUES ('p', ?, ?, 'running', ?)",
            (task_id, now, str(Path("nul"))),
        )
    conn.commit()


def test_ancestors_keep_set(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    conn = open_db(db)
    _seed(conn, task_id="a")
    _seed(conn, task_id="b", depends_on=["a"])
    _seed(conn, task_id="c", depends_on=["b"])
    _seed(conn, task_id="d", depends_on=["c"])
    keep = collect_ancestors_to_root(conn, "p", "c", root="a")
    assert keep == {"a", "b", "c"}
    assert collect_downstream_task_ids(conn, "p", "a") == ["b", "c", "d"]
    assert collect_ancestors_to_root(conn, "p", "c", root="d") is None
    conn.close()


def test_lock_occupancy(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    conn = open_db(db)
    _seed(
        conn,
        task_id="holder",
        status="running",
        resources={"locks": ["corpus"]},
    )
    _seed(
        conn,
        task_id="waiter",
        status="queued",
        resources={"locks": ["corpus"]},
        last_error_code="tq.block.mutex",
    )
    occ = lock_occupancy(conn)
    assert occ["corpus"]["holder"] == "p/holder"
    assert "p/waiter" in occ["corpus"]["waiters"]
    conn.close()


def test_lock_contention_summary_below_project_limit(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    conn = open_db(db)
    set_project_limit(conn, "p", "max_tasks", 3.0)
    _seed(
        conn,
        task_id="holder",
        status="running",
        resources={"locks": ["corpus"]},
    )
    _seed(
        conn,
        task_id="waiter",
        status="queued",
        resources={"locks": ["corpus"]},
        last_error_code="tq.block.mutex",
    )
    summary = lock_contention_summary(conn)
    assert len(summary) == 1
    row = summary[0]
    assert row["lock"] == "corpus"
    assert row["holder"] == "p/holder"
    assert row["waiter_count"] == 1
    assert row["project"] == "p"
    assert row["running"] == 1
    assert row["max_tasks"] == 3
    assert row["headroom"] == 2
    assert row["below_project_limit"] is True
    conn.close()
