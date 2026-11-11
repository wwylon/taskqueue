"""F-231: the two-phase queue page returns the same rows, in the same order, as a full sort."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tqdaemon.db import open_db
from tqdaemon.web.responses import _get_queue_response, _queue_order_by


def _seed(conn, n: int = 120) -> None:
    statuses = ["completed", "queued", "failed", "running", "stopped", "disabled"]
    for i in range(n):
        proj = f"p{i % 3}"
        conn.execute(
            "INSERT INTO tasks (project, task_id, command, status, priority, enqueued_at, updated_at, description, "
            "tags) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                proj,
                f"t{i:03d}",
                "x",
                statuses[i % 6],
                i % 4,
                f"2026-01-01T00:{i % 7:02d}:00+00:00",
                f"2026-01-02T00:{i % 11:02d}:00+00:00",
                f"d{i % 5}",
                json.dumps([f"tag{i % 2}"]),
            ),
        )
        if i % 2:
            cur = conn.execute(
                "INSERT INTO runs (project, task_id, status, started_at, finished_at, worker_id) VALUES (?,?,?,?,?,?)",
                (proj, f"t{i:03d}", "completed", f"2026-01-03T{i % 13:02d}:00:00+00:00", None, f"w{i % 2}"),
            )
            conn.execute(
                "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?", (cur.lastrowid, proj, f"t{i:03d}")
            )
    conn.commit()


def _expected(conn, sort, sort_dir, limit, offset, where="", params=()):
    order = f"{_queue_order_by(sort, sort_dir)}, t.rowid"
    sql = (
        "SELECT t.project, t.task_id FROM tasks t LEFT JOIN runs lr ON lr.run_id = t.last_run_id "
        f"{where} ORDER BY {order} LIMIT ? OFFSET ?"
    )
    return [(r[0], r[1]) for r in conn.execute(sql, (*params, limit, offset))]


@pytest.mark.parametrize(
    "sort",
    [
        "status",
        "task_id",
        "project",
        "priority",
        "enqueued_at",
        "started_at",
        "finished_at",
        "description",
        "queue_pos",
        "yaml_num",
    ],
)
@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
def test_two_phase_page_matches_full_sort(tmp_path: Path, sort, sort_dir) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db)
    for offset in (0, 25, 110):
        got = [
            (t["project"], t["task_id"])
            for t in _get_queue_response(db, sort=sort, sort_dir=sort_dir, limit=25, offset=offset)["tasks"]
        ]
        assert got == _expected(db, sort, sort_dir, 25, offset)


def test_two_phase_with_filters(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db)
    got = _get_queue_response(db, project="p1", worker="w1", sort="started_at", sort_dir="desc", limit=10)
    exp = _expected(db, "started_at", "desc", 10, 0, "WHERE t.project = ? AND lr.worker_id = ?", ("p1", "w1"))
    assert [(t["project"], t["task_id"]) for t in got["tasks"]] == exp
    assert (
        got["total"]
        == db.execute(
            "SELECT COUNT(*) FROM tasks t LEFT JOIN runs lr ON lr.run_id = t.last_run_id "
            "WHERE t.project='p1' AND lr.worker_id='w1'"
        ).fetchone()[0]
    )
    assert all(t["last_worker_id"] == "w1" for t in got["tasks"])
