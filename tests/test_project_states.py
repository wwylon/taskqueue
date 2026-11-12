"""Queue project chip colours: each project's worst live state."""

from __future__ import annotations

from tqdaemon.db import open_db
from tqdaemon.queries import project_states


def test_project_state_precedence(tmp_path):
    db = open_db(tmp_path / "q.db")
    rows = [
        ("a", "t1", "completed"),
        ("a", "t2", "running"),
        ("b", "t1", "queued"),
        ("b", "t2", "completed"),
        ("c", "t1", "failed"),
        ("c", "t2", "running"),
        ("d", "t1", "stopped"),
        ("e", "t1", "disabled"),
    ]
    for proj, tid, st in rows:
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, args, enqueued_at, updated_at) "
            "VALUES (?, ?, ?, 'x', '[]', '2026-01-01', '2026-01-01')",
            (proj, tid, st),
        )
    db.commit()
    assert project_states(db) == {"a": "running", "b": "waiting", "c": "failed", "d": "interrupted"}
