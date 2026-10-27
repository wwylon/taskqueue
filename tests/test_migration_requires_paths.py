"""Migration A1 normalizes requires_paths per (project, task_id) only."""

from __future__ import annotations

import json
from pathlib import Path

from taskqueue.db import open_db


def _paths(db, project: str, task_id: str) -> list[str]:
    row = db.execute(
        "SELECT requires_paths FROM tasks WHERE project=? AND task_id=?", (project, task_id)
    ).fetchone()
    return json.loads(row["requires_paths"])


def test_a1_scoped_to_project(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.db"
    db = open_db(db_path)
    for project, paths in (("a", ["C:\\data\\x.bin"]), ("b", ["rel/keep.txt"])):
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, requires_paths) "
            "VALUES (?, 'same', 'queued', 'echo', '2026-01-01 00:00:00', '2026-01-01 00:00:00', ?)",
            (project, json.dumps(paths)),
        )
    db.execute("UPDATE config SET value = '0' WHERE key = 'schema_version'")  # force re-migration
    db.commit()
    db.close()

    db = open_db(db_path, migrate=True)
    assert _paths(db, "a", "same") == ["C:/data/x.bin"]
    # Same task_id in another project must be untouched.
    assert _paths(db, "b", "same") == ["rel/keep.txt"]
