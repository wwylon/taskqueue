"""PR2: retry skip reasons, downstream, sync offline exit codes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue._cli_project import cmd_sync
from taskqueue._db_tasks import (
    collect_downstream_task_ids,
    retry_skip_reason,
    retry_task,
    retry_task_with_downstream,
)
from taskqueue.db import open_db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seed(
    conn,
    *,
    task_id: str,
    status: str = "completed",
    enabled: int = 1,
    depends_on: list | None = None,
) -> None:
    import json

    now = _now()
    conn.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, enabled, priority, "
        "resources, depends_on, exclusive, max_attempts, enqueued_at, updated_at) "
        "VALUES ('p', ?, ?, 'echo', '[]', ?, 50, '{}', ?, 0, 0, ?, ?)",
        (task_id, status, enabled, json.dumps(depends_on or []), now, now),
    )
    conn.commit()


def test_retry_skips_disabled(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    conn = open_db(db)
    _seed(conn, task_id="a", status="completed", enabled=0)
    assert retry_skip_reason(conn, "p", "a") == "disabled"
    assert retry_task(conn, "p", "a") is False
    row = conn.execute("SELECT status FROM tasks WHERE task_id='a'").fetchone()
    assert row["status"] == "completed"
    conn.close()


def test_downstream_skip_report(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    conn = open_db(db)
    _seed(conn, task_id="root", status="completed", enabled=1)
    _seed(conn, task_id="child", status="completed", enabled=0, depends_on=["root"])
    _seed(conn, task_id="ok", status="completed", enabled=1, depends_on=["root"])
    assert collect_downstream_task_ids(conn, "p", "root") == ["child", "ok"]
    result = retry_task_with_downstream(conn, "p", "root")
    assert "root" in result["requeued"]
    assert "ok" in result["requeued"]
    skipped = {s["task_id"]: s["reason"] for s in result["skipped"]}
    assert skipped.get("child") == "disabled"
    conn.close()


def test_sync_require_daemon_exit_3(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr("taskqueue._cli_project._daemon_running", lambda h: None)
    monkeypatch.setattr("taskqueue._cli_project._db", lambda h: home / "queue.db")
    rc = cmd_sync(home, require_daemon=True)
    assert rc == 3
    err = capsys.readouterr().err
    assert "[OFFLINE]" in err
    assert "require-daemon" in err


def test_sync_refuses_sqlite_when_daemon_up_but_http_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Timeout / busy master must not fall back to offline dual-write."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr("taskqueue._cli_project._daemon_running", lambda h: 42)
    rc = cmd_sync(home, require_daemon=False)
    assert rc == 3
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert "Refusing offline" in err
    assert "[OFFLINE]" not in err
