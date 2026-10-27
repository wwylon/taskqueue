"""POST /api/tasks/bulk: many tasks in one request, per-task results."""
from __future__ import annotations

from tests.helpers.master_worker import (
    http_request,
    make_handler_env,
    seed_queued_task,
)


def _status(db, tid):
    return db.execute("SELECT status, priority, enabled FROM tasks WHERE task_id=?", (tid,)).fetchone()


def _env(tmp_path):
    handler, db, _home = make_handler_env(tmp_path)
    for tid in ("A", "B", "C"):
        seed_queued_task(db, task_id=tid)
    db.commit()
    return handler, db


def test_bulk_stop_queued(tmp_path):
    handler, db = _env(tmp_path)
    status, body = http_request(
        handler, "POST", "/api/tasks/bulk",
        {"action": "stop", "tasks": ["proj/A", "proj/B"]},
    )
    assert status == 200 and body["total"] == 2 and body["failed"] == 0
    assert _status(db, "A")["status"] == "interrupted"
    assert _status(db, "B")["status"] == "interrupted"
    assert _status(db, "C")["status"] == "queued"


def test_bulk_priority_and_pause_resume(tmp_path):
    handler, db = _env(tmp_path)
    status, body = http_request(
        handler, "POST", "/api/tasks/bulk",
        {"action": "priority", "tasks": ["proj/A", "proj/C"], "priority": 7},
    )
    assert status == 200 and body["ok"]
    assert _status(db, "A")["priority"] == 7 and _status(db, "C")["priority"] == 7
    status, body = http_request(
        handler, "POST", "/api/tasks/bulk",
        {"action": "pause", "tasks": ["proj/A"]},
    )
    assert status == 200 and body["ok"] and _status(db, "A")["enabled"] == 0
    status, body = http_request(
        handler, "POST", "/api/tasks/bulk",
        {"action": "resume", "tasks": ["proj/A"]},
    )
    assert status == 200 and _status(db, "A")["enabled"] == 1


def test_bulk_rejects_bad_input(tmp_path):
    handler, _db = _env(tmp_path)
    for payload in (
        {"action": "nope", "tasks": ["proj/A"]},
        {"action": "stop", "tasks": []},
        {"action": "stop", "tasks": ["no-slash"]},
        {"action": "priority", "tasks": ["proj/A"], "priority": 101},
        {"action": "priority", "tasks": ["proj/A"]},
    ):
        status, _ = http_request(handler, "POST", "/api/tasks/bulk", payload)
        assert status == 400, payload


def test_bulk_failure_is_per_task(tmp_path):
    handler, db = _env(tmp_path)
    status, body = http_request(
        handler, "POST", "/api/tasks/bulk",
        {"action": "pause", "tasks": ["proj/A", "proj/MISSING", "proj/B"]},
    )
    assert status == 200 and body["total"] == 3
    assert body["failed"] >= 1 and body["ok"] is False
    assert _status(db, "A")["enabled"] == 0 and _status(db, "B")["enabled"] == 0
