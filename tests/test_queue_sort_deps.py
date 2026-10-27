"""sort=deps puts the biggest blockers first and pages correctly; fetching only the page keeps rows intact."""

from __future__ import annotations

from pathlib import Path

from taskqueue.db import open_db
from taskqueue.web.responses import _get_queue_response


def _ins(conn, tid: str, deps: str = "[]", status: str = "queued") -> None:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, depends_on) "
        "VALUES ('p', ?, 'x', ?, '2026-10-23T00:00:00', '2026-10-23T00:00:00', ?)",
        (tid, status, deps),
    )


def _seed(conn) -> None:
    for tid in ("leaf", "mid", "root"):
        _ins(conn, tid, deps={"leaf": "[]", "mid": '["root"]', "root": "[]"}[tid])
    _ins(conn, "c1", deps='["root"]')
    _ins(conn, "c2", deps='["root", "mid"]')
    cur = conn.execute(
        "INSERT INTO runs (project, task_id, status, started_at, worker_id) "
        "VALUES ('p', 'root', 'completed', '2026-10-23T00:00:00', 'w1')"
    )
    conn.execute(
        "UPDATE tasks SET last_run_id = ? WHERE project = 'p' AND task_id = 'root'",
        (cur.lastrowid,),
    )
    conn.commit()


def test_biggest_blocker_first_and_pages_stay_consistent(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "tq.db")
    _seed(conn)
    full = _get_queue_response(conn, sort="deps", limit=50)
    ids = [t["task_id"] for t in full["tasks"]]
    assert full["total"] == 5 and ids[0] == "root" and ids[1] == "mid"  # root blocks 3, mid blocks 1
    page = _get_queue_response(conn, sort="deps", limit=2, offset=1)
    assert [t["task_id"] for t in page["tasks"]] == ids[1:3]
    assert page["total"] == 5
    root = full["tasks"][0]
    assert root["last_worker_id"] == "w1" and root["started_at"] == "2026-10-23T00:00:00"
    conn.close()
