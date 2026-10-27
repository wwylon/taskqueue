"""GET/POST /api/transfer/* — push and pull file sync (F-065 scoped)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from taskqueue.config import Project
from tests.helpers.master_worker import http_request, make_handler_env, seed_master_lease


def _seed_running_transfer(
    db,
    *,
    project: str = "proj",
    task_id: str = "t1",
    transfer: list | None = None,
    token: str = "tok-abc",
) -> int:
    now = datetime.now(UTC).isoformat()
    xfer = json.dumps(transfer or [{"src": "input.txt", "dst": "input.txt", "direction": "push"}])
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, enabled, enqueued_at, updated_at, transfer) "
        "VALUES (?, ?, 'running', 'true', '[]', 'remote', 50, '{}', '[]', 0, 1, ?, ?, ?)",
        (project, task_id, now, now, xfer),
    )
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, finalize_token, worker_id) "
        "VALUES (?, ?, ?, 'running', ?, 'w1')",
        (project, task_id, now, token),
    )
    db.commit()
    return int(cur.lastrowid)


def test_transfer_push_get_serves_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    root = tmp_path / "projroot"
    root.mkdir()
    (root / "input.txt").write_text("hello-push", encoding="utf-8")
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    seed_master_lease(db)
    run_id = _seed_running_transfer(db)
    code, data = http_request(
        HandlerClass,
        "GET",
        f"/api/transfer/get/proj/input.txt?run_id={run_id}&finalize_token=tok-abc",
    )
    assert code == 200
    assert data == b"hello-push"
    db.close()


def test_transfer_requires_run_capability(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    root = tmp_path / "projroot"
    root.mkdir()
    (root / "input.txt").write_text("secret", encoding="utf-8")
    (root / "other.txt").write_text("nope", encoding="utf-8")
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    seed_master_lease(db)
    run_id = _seed_running_transfer(db)
    # Missing credentials
    code, _ = http_request(HandlerClass, "GET", "/api/transfer/get/proj/input.txt")
    assert code == 401
    # Wrong token
    code, _ = http_request(
        HandlerClass,
        "GET",
        f"/api/transfer/get/proj/input.txt?run_id={run_id}&finalize_token=wrong",
    )
    assert code == 403
    # Path not in transfer: manifest
    code, _ = http_request(
        HandlerClass,
        "GET",
        f"/api/transfer/get/proj/other.txt?run_id={run_id}&finalize_token=tok-abc",
    )
    assert code == 403
    db.close()


def test_transfer_push_path_escape_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    root = tmp_path / "projroot"
    root.mkdir()
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    seed_master_lease(db)
    run_id = _seed_running_transfer(
        db,
        transfer=[{"src": "../outside.txt", "direction": "push"}],
    )
    code, data = http_request(
        HandlerClass,
        "GET",
        f"/api/transfer/get/proj/../outside.txt?run_id={run_id}&finalize_token=tok-abc",
    )
    assert code == 403
    db.close()


def test_transfer_pull_post_writes_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    root = tmp_path / "projroot"
    root.mkdir()
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    seed_master_lease(db)
    run_id = _seed_running_transfer(
        db,
        transfer=[{"src": "out.txt", "dst": "output/result.txt", "direction": "pull"}],
    )
    body = b"pulled-by-worker"
    code, data = http_request(
        HandlerClass,
        "POST",
        f"/api/transfer/put/proj/output/result.txt?run_id={run_id}&finalize_token=tok-abc",
        body,
        extra_headers={"Content-Type": "application/octet-stream"},
    )
    assert code == 200
    assert (root / "output" / "result.txt").read_bytes() == body
    db.close()


def test_transfer_pull_rejects_oversize(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    root = tmp_path / "projroot"
    root.mkdir()
    projects = {"proj": Project(name="proj", root=root, queue_dir=root / "q")}
    HandlerClass, db, _ = make_handler_env(tmp_path, projects=projects)
    seed_master_lease(db)
    run_id = _seed_running_transfer(
        db,
        transfer=[{"src": "big.bin", "dst": "big.bin", "direction": "pull"}],
    )
    too_big = 64 * 1024 * 1024 + 1
    code, data = http_request(
        HandlerClass,
        "POST",
        f"/api/transfer/put/proj/big.bin?run_id={run_id}&finalize_token=tok-abc",
        b"x",
        extra_headers={"Content-Length": str(too_big), "Content-Type": "application/octet-stream"},
    )
    assert code == 400
    db.close()
