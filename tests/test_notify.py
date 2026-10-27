"""Opt-in task-end notify (notify_url / notify.jsonl / webhook)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from taskqueue.config import Project
from taskqueue.db import SCHEMA_VERSION, get_config, open_db, parse_task_yaml
from taskqueue._db_tasks import upsert_task
from taskqueue.mcp_server import TOOLS, _call_tool
from taskqueue.models import STATUS_COMPLETED, STATUS_FAILED
from taskqueue.notify import (
    drain_notifications,
    emit_task_notify,
    notify_log_path,
    validate_notify_url,
)
from taskqueue.runner import ActiveRun


def _proj(tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "bin").mkdir()
    (root / "bin" / "true.py").write_text("print('ok')\n", encoding="utf-8")
    return Project(
        name="p",
        root=root,
        queue_dir=root / "q",
    )


def _write_yaml(path: Path, *, notify_url: str | None = None) -> None:
    lines = [
        "task_id: t1",
        "command: bin/true.py",
        "args: []",
        "priority: 50",
    ]
    if notify_url is not None:
        lines.append(f"notify_url: {notify_url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_schema_version_has_notify() -> None:
    assert SCHEMA_VERSION >= 8


def test_validate_notify_url_loopback() -> None:
    assert validate_notify_url("http://127.0.0.1:9/hook") == "http://127.0.0.1:9/hook"
    assert validate_notify_url("http://localhost/x") == "http://localhost/x"
    with pytest.raises(ValueError, match="loopback"):
        validate_notify_url("http://example.com/hook")
    assert validate_notify_url(
        "http://example.com/hook", allow_remote=True
    ) == "http://example.com/hook"
    with pytest.raises(ValueError, match="http"):
        validate_notify_url("ftp://127.0.0.1/x")


def test_parse_notify_url(tmp_path: Path) -> None:
    proj = _proj(tmp_path)
    path = proj.queue_dir / "t1.yaml"
    _write_yaml(path, notify_url="http://127.0.0.1:8765/hook")
    task = parse_task_yaml(path, proj)
    assert task.notify_url == "http://127.0.0.1:8765/hook"

    _write_yaml(path, notify_url="http://evil.example/hook")
    with pytest.raises(ValueError, match="loopback"):
        parse_task_yaml(path, proj)
    task2 = parse_task_yaml(path, proj, allow_remote_notify=True)
    assert task2.notify_url == "http://evil.example/hook"


def test_upsert_persists_notify_url(tmp_path: Path) -> None:
    proj = _proj(tmp_path)
    path = proj.queue_dir / "t1.yaml"
    _write_yaml(path, notify_url="http://127.0.0.1/n")
    db = open_db(tmp_path / "q.db")
    upsert_task(db, parse_task_yaml(path, proj))
    row = db.execute(
        "SELECT notify_url FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()
    assert row["notify_url"] == "http://127.0.0.1/n"
    assert get_config(db).get("notify_allow_remote") in (0, False)
    db.close()


def test_emit_jsonl_and_skip_without_url_when_default_off(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert emit_task_notify(
        home,
        project="p",
        task_id="t",
        status=STATUS_COMPLETED,
        run_id=1,
        exit_code=0,
        last_error_code=None,
        notify_url=None,
        started_by="alice",
        post=False,
        cfg={"notify_default": 0, "toast_notifications": False},
    ) is None
    assert not notify_log_path(home).exists()

    payload = emit_task_notify(
        home,
        project="p",
        task_id="t",
        status=STATUS_COMPLETED,
        run_id=1,
        exit_code=0,
        last_error_code=None,
        notify_url="http://127.0.0.1/n",
        started_by="alice",
        post=False,
    )
    assert payload is not None
    lines = notify_log_path(home).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["task_id"] == "t" and ev["status"] == "completed"

    # non-terminal
    assert emit_task_notify(
        home,
        project="p",
        task_id="t",
        status="queued",
        run_id=2,
        exit_code=None,
        last_error_code=None,
        notify_url="http://127.0.0.1/n",
        started_by="alice",
        post=False,
    ) is None


def test_drain_notifications_since_and_operator(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    emit_task_notify(
        home, project="p", task_id="a", status="completed", run_id=1,
        exit_code=0, last_error_code=None, notify_url="http://127.0.0.1/n",
        started_by="alice", post=False,
    )
    first_ts = json.loads(
        notify_log_path(home).read_text(encoding="utf-8").splitlines()[0]
    )["ts"]
    emit_task_notify(
        home, project="p", task_id="b", status="failed", run_id=2,
        exit_code=1, last_error_code="tq.run.nonzero",
        notify_url="http://127.0.0.1/n", started_by="bob", post=False,
    )
    all_ev = drain_notifications(home)
    assert all_ev["count"] == 2
    alice = drain_notifications(home, operator="alice")
    assert alice["count"] == 1 and alice["events"][0]["task_id"] == "a"
    # ts < since excludes; bump past first event
    only_second = drain_notifications(home, since=first_ts + "\uffff")
    assert only_second["count"] == 1
    assert only_second["events"][0]["task_id"] == "b"


def test_webhook_post_on_emit(tmp_path: Path) -> None:
    received: list[dict] = []
    ready = threading.Event()

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            received.append(json.loads(body.decode("utf-8")))
            self.send_response(200)
            self.end_headers()
            ready.set()

        def log_message(self, *args) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        home = tmp_path / "home"
        home.mkdir()
        emit_task_notify(
            home,
            project="p",
            task_id="t",
            status=STATUS_FAILED,
            run_id=9,
            exit_code=1,
            last_error_code="tq.run.nonzero",
            notify_url=f"http://127.0.0.1:{port}/hook",
            started_by="alice",
            post=True,
        )
        assert ready.wait(timeout=5.0)
        assert received[0]["task_id"] == "t"
        assert received[0]["status"] == "failed"
    finally:
        httpd.shutdown()


def test_finalize_emits_notify(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "queue.db"
    db = open_db(db_path)
    proj = _proj(tmp_path)
    yml = proj.queue_dir / "t1.yaml"
    _write_yaml(yml, notify_url="http://127.0.0.1/n")
    upsert_task(db, parse_task_yaml(yml, proj))
    db.execute(
        "UPDATE tasks SET status='running', started_by='alice' "
        "WHERE project='p' AND task_id='t1'"
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()
    run = ActiveRun(db, row, log_dir=home / "logs", cwd=proj.root, db_path=db_path)
    run.run_id = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status) "
        "VALUES ('p','t1', '2026-01-01T00:00:00+00:00', 'running')"
    ).lastrowid
    db.commit()
    run._exit_code = 0
    run.finalize(STATUS_COMPLETED)
    ev = drain_notifications(home)
    assert ev["count"] == 1
    assert ev["events"][0]["started_by"] == "alice"
    db.close()


def test_worker_finalize_emits_once(tmp_path: Path) -> None:
    from tests.helpers.master_worker import http_request, make_handler_env

    home = tmp_path / "home"
    home.mkdir()
    HandlerClass, db, home = make_handler_env(home)
    proj = _proj(tmp_path)
    yml = proj.queue_dir / "t1.yaml"
    _write_yaml(yml, notify_url="http://127.0.0.1/n")
    upsert_task(db, parse_task_yaml(yml, proj))
    db.execute(
        "UPDATE tasks SET status='running', started_by='bob' WHERE project='p' AND task_id='t1'"
    )
    run_id = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, worker_id) "
        "VALUES ('p','t1','2026-01-01T00:00:00+00:00','running','w1')"
    ).lastrowid
    db.commit()

    body = {
        "run_id": run_id,
        "worker_id": "w1",
        "exit_code": 0,
        "status": "completed",
    }
    code, data = http_request(HandlerClass, "POST", "/api/worker/finalize", body)
    assert code == 200
    assert data == {"ok": True}
    assert drain_notifications(home)["count"] == 1

    # second finalize should not emit again
    code2, data2 = http_request(HandlerClass, "POST", "/api/worker/finalize", body)
    assert data2["ok"] is False
    assert drain_notifications(home)["count"] == 1
    db.close()


def test_mcp_drain_notifications(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    open_db(home / "queue.db").close()
    emit_task_notify(
        home, project="p", task_id="t", status="completed", run_id=1,
        exit_code=0, last_error_code=None, notify_url="http://127.0.0.1/n",
        started_by="alice", post=False,
    )
    names = {t["name"] for t in TOOLS}
    assert "drain_notifications" in names
    out = _call_tool(home, "drain_notifications", {"operator": "alice"})
    assert out["count"] == 1
