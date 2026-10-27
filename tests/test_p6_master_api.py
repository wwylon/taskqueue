"""P6 / R-010: CLI mutations via master HTTP; SQLite fallback when unreachable."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue.db import parse_task_yaml
from taskqueue._db_tasks import upsert_task
from taskqueue.master_client import via_master
from tests.helpers.master_worker import http_request, make_handler_env


def test_via_master_none_when_unreachable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TQ_MASTER", raising=False)
    assert via_master(tmp_path, "GET", "/api/status") is None
    monkeypatch.setenv("TQ_MASTER", "http://127.0.0.1:1")
    assert via_master(tmp_path, "GET", "/api/status") is None


def test_via_master_forwards_timeout(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def _fake_api(home, method, path, *, body=None, timeout=8.0):
        captured["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setenv("TQ_MASTER", "http://127.0.0.1:9")
    monkeypatch.setattr("taskqueue.master_client.api_request", _fake_api)
    assert via_master(tmp_path, "POST", "/api/cli/sync", timeout=123.0) == {"ok": True}
    assert captured["timeout"] == 123.0


def test_cli_add_and_patch_via_handler(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    yml = qdir / "job.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "job",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "priority": 40,
        }),
        encoding="utf-8",
    )
    proj = Project(name="p", root=root, queue_dir=qdir)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"p": proj})
    upsert_task(db, parse_task_yaml(yml, proj))

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/cli/add",
        {
            "project": "p",
            "filename": "job2.yaml",
            "yaml_text": yaml.dump({
                "task_id": "job2",
                "command": sys.executable,
                "args": ["-c", "pass"],
            }),
        },
    )
    assert code == 200
    assert data["ok"] is True
    assert "job2" in data["task_ids"]

    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/tasks/p/job/patch",
        {"updates": {"priority": 77}},
    )
    assert code == 200
    assert data["ok"] is True
    row = db.execute("SELECT priority FROM tasks WHERE task_id='job'").fetchone()
    assert int(row["priority"]) == 77

    code, data = http_request(HandlerClass, "POST", "/api/cli/sync")
    assert code == 200
    assert data["ok"] is True
    assert data["total"] >= 2
    db.close()


def test_retry_via_http_handler(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    HandlerClass, db, _ = make_handler_env(tmp_path)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES ('p','t1','python','failed',?,?)",
        (now, now),
    )
    db.commit()
    code, data = http_request(HandlerClass, "POST", "/api/tasks/p/t1/retry", {})
    assert code == 200
    assert data["ok"] is True
    st = db.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()["status"]
    assert st == "queued"
    db.close()
