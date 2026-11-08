"""F-245 dual-writer gate via mutate; F-246 MCP offline queued stop → interrupted."""

from __future__ import annotations

import time
from pathlib import Path

from taskqueue.db import open_db


def _home_with_lease(tmp_path: Path, monkeypatch) -> Path:
    from taskqueue import _cli_helpers as h

    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "queue.db"
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    monkeypatch.delenv("TQ_MASTER", raising=False)
    monkeypatch.setattr(h, "_db", lambda _home: db_path)
    monkeypatch.setattr(h, "_daemon_running", lambda _home: None)
    db = open_db(db_path)
    db.execute(
        "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at) VALUES (1, ?, ?)",
        ("other-host", int(time.time()) + 600),
    )
    db.commit()
    db.close()
    return home


def test_f245_stop_op_blocked_when_lease_held(tmp_path: Path, monkeypatch):
    from taskqueue.mutations import stop_op

    home = _home_with_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "taskqueue.master_client.via_master",
        lambda *a, **k: None,
    )
    resp = stop_op(home, "proj", "T1")
    assert resp.get("ok") is False
    assert resp.get("unreachable") is True
    assert "master_lease" in (resp.get("error") or "")


def test_f246_stop_op_offline_queued_to_interrupted(tmp_path: Path, monkeypatch):
    from taskqueue import _cli_helpers as h
    from taskqueue.mutations import stop_op

    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "queue.db"
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    monkeypatch.delenv("TQ_MASTER", raising=False)
    monkeypatch.setattr(h, "_db", lambda _home: db_path)
    monkeypatch.setattr(h, "_daemon_running", lambda _home: None)
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)

    db = open_db(db_path)
    now = "2026-11-07T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, exclusive_project, max_attempts, enabled, "
        "enqueued_at, updated_at) "
        "VALUES ('proj', 'T1', 'queued', 'true', '[]', 'local', 50, '{}', '[]', 0, 0, 0, 1, ?, ?)",
        (now, now),
    )
    db.commit()
    db.close()

    resp = stop_op(home, "proj", "T1")
    assert resp.get("ok") is True
    assert resp.get("status") == "interrupted" or resp.get("final_status") == "interrupted"

    db = open_db(db_path, migrate=False)
    row = db.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    db.close()
    assert row["status"] == "interrupted"
