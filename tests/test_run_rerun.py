"""POST /api/runs/<id>/rerun re-queues a task with a past run's recorded args."""
from __future__ import annotations

from tests.helpers.master_worker import http_request, make_handler_env


def _seed(db, *, args_json='["--a=1","--epochs=5"]', status="failed"):
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, overridable_args, status, "
        "enabled, enqueued_at, updated_at) VALUES "
        "('p','t','x','[\"--a=1\"]','[\"--epochs\"]',?,1,'n','n')",
        (status,),
    )
    db.execute(
        "INSERT INTO runs (run_id, project, task_id, status, started_at) "
        "VALUES (1,'p','t','failed','n')"
    )
    # An insert trigger snapshots the task's args; override it with the run's own.
    db.execute("UPDATE runs SET args_json = ? WHERE run_id = 1", (args_json,))
    db.commit()


def test_rerun_requeues_with_recorded_args(tmp_path):
    handler, db, _ = make_handler_env(tmp_path)
    _seed(db)
    status, body = http_request(handler, "POST", "/api/runs/1/rerun")
    assert status == 200 and body["ok"], body
    row = db.execute("SELECT status, args_override FROM tasks WHERE task_id='t'").fetchone()
    assert row["status"] == "queued"
    assert row["args_override"] == '["--a=1", "--epochs=5"]'


def test_rerun_rejects_args_outside_allowlist(tmp_path):
    handler, db, _ = make_handler_env(tmp_path)
    _seed(db, args_json='["--evil=1"]')
    status, body = http_request(handler, "POST", "/api/runs/1/rerun")
    assert status == 409 and not body["ok"]
    row = db.execute("SELECT status, args_override FROM tasks WHERE task_id='t'").fetchone()
    assert row["status"] == "failed" and row["args_override"] is None


def test_rerun_without_snapshot_and_unknown_run(tmp_path):
    handler, db, _ = make_handler_env(tmp_path)
    _seed(db, args_json=None)
    assert http_request(handler, "POST", "/api/runs/1/rerun")[0] == 409
    assert http_request(handler, "POST", "/api/runs/99/rerun")[0] == 404
    assert http_request(handler, "POST", "/api/runs/abc/rerun")[0] == 400


def test_rerun_not_retryable_clears_override(tmp_path):
    handler, db, _ = make_handler_env(tmp_path)
    _seed(db, status="running")
    status, body = http_request(handler, "POST", "/api/runs/1/rerun")
    assert status == 409 and not body["ok"]
    row = db.execute("SELECT args_override FROM tasks WHERE task_id='t'").fetchone()
    assert row["args_override"] is None
