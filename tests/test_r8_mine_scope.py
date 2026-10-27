"""R8: started_by queue/history filters + /api/my-projects."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.queries import list_operator_projects
from taskqueue.web.responses import _get_history_response, _get_queue_response
from tests.test_web_routes import _make_handler, _make_request


def _seed(db, *, project="proj", task_id="T1", status="queued", started_by=None, submitted_by=None):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, started_by, submitted_by) VALUES (?, ?, ?, 'true', ?, ?, 1, ?, ?)",
        (project, task_id, status, now, now, started_by, submitted_by),
    )
    db.commit()


def _seed_run(db, *, project="proj", task_id="T1", run_id=1):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (run_id, project, task_id, status, started_at, finished_at) "
        "VALUES (?, ?, ?, 'completed', ?, ?)",
        (run_id, project, task_id, now, now),
    )
    db.commit()


def test_queue_filters_by_started_by(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, task_id="mine", started_by="ada")
    _seed(db, task_id="theirs", started_by="bob")
    _seed(db, task_id="orphan", started_by=None)
    all_q = _get_queue_response(db, limit=50)
    assert all_q["total"] == 3
    mine = _get_queue_response(db, started_by="ada", limit=50)
    assert mine["total"] == 1
    assert mine["tasks"][0]["task_id"] == "mine"


def test_history_filters_by_started_by(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, task_id="mine", started_by="ada")
    _seed(db, task_id="theirs", started_by="bob")
    _seed_run(db, task_id="mine", run_id=1)
    _seed_run(db, task_id="theirs", run_id=2)
    all_h = _get_history_response(db, limit=50)
    assert all_h["total"] == 2
    mine = _get_history_response(db, started_by="ada", limit=50)
    assert mine["total"] == 1
    assert mine["runs"][0]["task_id"] == "mine"


def test_list_operator_projects_started_or_submitted(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    _seed(db, project="a", task_id="t1", started_by="ada")
    _seed(db, project="b", task_id="t2", submitted_by="ada")
    _seed(db, project="c", task_id="t3", started_by="bob")
    assert list_operator_projects(db, operator="ada") == ["a", "b"]
    assert list_operator_projects(db, operator="") == []


def test_api_my_projects_and_queue_started_by(tmp_path: Path) -> None:
    HandlerClass, db = _make_handler(tmp_path)
    _seed(db, project="alpha", task_id="t1", started_by="ada")
    _seed(db, project="beta", task_id="t2", started_by="bob")
    code, body = _make_request(HandlerClass, "GET", "/api/my-projects?operator=ada")
    assert code == 200
    data = json.loads(body)
    assert data["operator"] == "ada"
    assert data["projects"] == ["alpha"]

    code, body = _make_request(HandlerClass, "GET", "/api/queue?started_by=ada")
    assert code == 200
    data = json.loads(body)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    assert len(tasks) == 1 and tasks[0]["task_id"] == "t1"
