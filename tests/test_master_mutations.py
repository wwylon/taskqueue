"""Project pause/resume and task/project removal go through the master when a daemon runs."""
from __future__ import annotations

from pathlib import Path

from taskqueue import _cli_helpers
from taskqueue.db import open_db
from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task


def test_routes_pause_resume_remove_and_remove_project(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="a", dispatch="local")
    seed_queued_task(db, project="p", task_id="b", dispatch="local")
    assert http_request(H, "POST", "/api/project/pause/p")[1] == {"ok": True}
    assert db.execute("SELECT value FROM config WHERE key LIKE '%paused%p%'").fetchone() is not None
    assert http_request(H, "POST", "/api/project/resume/p")[1] == {"ok": True}
    status, body = http_request(H, "POST", "/api/remove/p/a")
    assert status == 200 and body["ok"] is True
    assert http_request(H, "POST", "/api/remove/p/a")[0] == 404
    db.execute("UPDATE tasks SET status='running' WHERE task_id='b'")
    db.commit()
    assert http_request(H, "POST", "/api/project/remove/p")[0] == 409  # running task, no force
    status, body = http_request(H, "POST", "/api/project/remove/p?force=1")
    assert status == 200 and body["tasks"] == 1
    actions = [r["action"] for r in db.execute("SELECT action FROM operator_audit ORDER BY id")]
    assert actions == ["pause", "resume", "remove", "remove_project"]


def test_cli_uses_master_refuses_when_unreachable_and_falls_back_offline(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskqueue import _cli_project

    (tmp_path / "projects.yaml").write_text(f"projects:\n  p:\n    root: {tmp_path.as_posix()}\n", encoding="utf-8")
    open_db(tmp_path / "queue.db").close()
    calls = []
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda h, m, path, **k: calls.append(path) or {"ok": True})
    assert _cli_project.cmd_pause(tmp_path, "p") == 0 and calls == ["/api/project/pause/p"]

    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr(_cli_helpers, "_daemon_running", lambda h: 4242)
    assert _cli_project.cmd_pause(tmp_path, "p") == 3
    assert "refusing a direct SQLite write" in capsys.readouterr().err

    monkeypatch.setattr(_cli_helpers, "_daemon_running", lambda h: None)
    assert _cli_project.cmd_pause(tmp_path, "p") == 0  # offline: direct write is fine
    from taskqueue.db import blocked_projects

    db = open_db(tmp_path / "queue.db")
    assert "p" in blocked_projects(db)
    db.close()


def test_stop_reason_is_recorded_by_the_master(tmp_path: Path) -> None:
    from taskqueue._db_runs import insert_run

    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    run_id = insert_run(db, "p", "t", str(tmp_path / "x.log"))
    db.execute("UPDATE tasks SET status='running'")
    db.commit()
    status, body = http_request(H, "POST", "/api/stop/p/t?reason=disk%20full")
    assert status == 200 and body["ok"] is True
    assert db.execute("SELECT stop_reason FROM runs WHERE run_id=?", (run_id,)).fetchone()[0] == "disk full"


def test_retry_batch_accepts_task_ids_only_and_bulk_stop(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    for t in ("a", "b"):
        seed_queued_task(db, project="p", task_id=t, dispatch="local")
    db.execute("UPDATE tasks SET status='failed'")
    db.commit()
    status, body = http_request(H, "POST", "/api/tasks/retry-batch", {"task_ids": ["p/a", "p/b"]})
    assert status == 200 and sorted(body["requeued"]) == ["p/a", "p/b"]
    status, body = http_request(H, "POST", "/api/tasks/bulk", {"action": "stop", "tasks": ["p/a", "p/b"]})
    assert status == 200 and body["total"] == 2 and body["failed"] == 0


def test_stop_records_requester_and_daemon_logs_it(tmp_path: Path, capsys) -> None:
    import json

    from taskqueue import daemon
    from taskqueue.config import control_path

    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    db.execute("UPDATE tasks SET status='running'")
    db.commit()
    status, _ = http_request(H, "POST", "/api/stop/p/t",
                             extra_headers={"X-TQ-Operator": "ada", "X-TQ-Via": "mcp", "X-TQ-Client": "mcp/cursor 1.0"})
    assert status == 200
    home = Path(H._db_path).parent
    data = json.loads(control_path(home, "p", "t").read_text(encoding="utf-8"))
    assert data["requested_by"] == "ada via mcp (mcp/cursor 1.0)"
    assert daemon._control_requester(home, "p", "t") == "ada via mcp (mcp/cursor 1.0)"
