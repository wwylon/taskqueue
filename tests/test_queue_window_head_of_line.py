import json

from taskqueue._db_tasks import get_queued_tasks
from taskqueue.db import open_db


def _ins(db, tid, prio, deps="[]", status="queued"):
    db.execute(
        "INSERT INTO tasks (project, task_id, status, enabled, priority, enqueued_at, updated_at, command, "
        "depends_on, depends_on_any, yaml_source) VALUES ('p', ?, ?, 1, ?, '2026-01-01', '2026-01-01', 'echo', ?, '[]', 'x.yaml')",
        (tid, status, prio, deps),
    )


def test_blocked_head_does_not_hide_runnable_tail(tmp_path) -> None:
    db = open_db(tmp_path / "q.db")
    _ins(db, "gate", 0, status="running")
    for i in range(5):
        _ins(db, f"blocked{i}", 90, json.dumps(["gate"]))
    _ins(db, "free", 1)
    db.commit()
    smap = {(r["project"], r["task_id"]): r["status"] for r in db.execute("SELECT * FROM tasks")}
    plain = [r["task_id"] for r in get_queued_tasks(db, limit=3)]
    assert "free" not in plain
    wide = [r["task_id"] for r in get_queued_tasks(db, limit=3, status_map=smap)]
    assert "free" in wide
    assert get_queued_tasks(db, limit=3, status_map=smap, skip_projects={"p"})[:3] == get_queued_tasks(db, limit=3)
