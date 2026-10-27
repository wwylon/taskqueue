"""F-056 finalize outbox/retry and F-057 master-lease fencing."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from taskqueue.db import holds_master_lease, open_db
from taskqueue.worker import (
    finalize_run,
    flush_pending_finalize,
    pending_finalize_dir,
    pending_finalize_is_ack,
    write_pending_finalize,
)
from tests.helpers.master_worker import (
    clear_master_lease,
    http_request,
    make_handler_env,
    seed_worker,
)


def _seed_lease(db, holder: str | None = None, ttl: int = 90) -> str:
    name = holder if holder is not None else socket.gethostname()
    db.execute(
        "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at) VALUES (1, ?, ?)",
        (name, int(time.time()) + ttl),
    )
    db.commit()
    return name


def _clear_lease(db) -> None:
    db.execute("DELETE FROM master_lease WHERE singleton=1")
    db.commit()


# ---------------------------------------------------------------------------
# F-056
# ---------------------------------------------------------------------------

def test_finalize_retries_when_post_returns_none(tmp_path: Path) -> None:
    """The old loop returned after the first `_post()` because it never raises."""
    calls: list[dict] = []

    def post_fn(url: str, data: dict):
        calls.append(data)
        if len(calls) < 3:
            return None
        return {"ok": True}

    sleeps: list[float] = []
    ok = finalize_run(
        tmp_path,
        "http://127.0.0.1:9",
        run_id=7,
        worker_id="w1",
        token="tok",
        extra={"status": "completed", "exit_code": 0},
        attempts=5,
        post_fn=post_fn,
        sleep_fn=sleeps.append,
    )
    assert ok is True
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    assert not (pending_finalize_dir(tmp_path) / "7.json").exists()


def test_pending_finalize_survives_failed_attempts(tmp_path: Path) -> None:
    def post_fn(url: str, data: dict):
        return None

    ok = finalize_run(
        tmp_path,
        "http://127.0.0.1:9",
        run_id=8,
        worker_id="w1",
        attempts=3,
        post_fn=post_fn,
        sleep_fn=lambda _s: None,
    )
    assert ok is False
    pending = pending_finalize_dir(tmp_path) / "8.json"
    assert pending.is_file()
    payload = json.loads(pending.read_text(encoding="utf-8"))
    assert payload["run_id"] == 8
    assert payload["worker_id"] == "w1"


def test_flush_pending_finalize_after_restart(tmp_path: Path) -> None:
    """T-032: leftover outbox is delivered on the next flush (simulates restart)."""
    write_pending_finalize(
        tmp_path,
        {"run_id": 9, "worker_id": "w1", "status": "completed", "exit_code": 0},
    )
    posts: list[dict] = []

    def fail(url: str, data: dict):
        posts.append(data)
        return None

    assert flush_pending_finalize(tmp_path, "http://127.0.0.1:9", post_fn=fail) == 1
    assert (pending_finalize_dir(tmp_path) / "9.json").is_file()

    def ok(url: str, data: dict):
        posts.append(data)
        return {"ok": True}

    assert flush_pending_finalize(tmp_path, "http://127.0.0.1:9", post_fn=ok) == 0
    assert not (pending_finalize_dir(tmp_path) / "9.json").exists()
    assert len(posts) == 2


def test_already_finalized_is_ack() -> None:
    assert pending_finalize_is_ack({"ok": False, "error": "run not running (already finalized or reclaimed)"})
    assert pending_finalize_is_ack(None) is False
    assert pending_finalize_is_ack({"ok": False, "error": "not master"}) is False


# ---------------------------------------------------------------------------
# F-057
# ---------------------------------------------------------------------------

def test_holds_master_lease_missing_and_expired(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    assert holds_master_lease(db) is False
    _seed_lease(db, holder="other-host")
    assert holds_master_lease(db) is False
    me = _seed_lease(db)
    assert holds_master_lease(db) is True
    assert holds_master_lease(db, holder=me, now=int(time.time()) + 10_000) is False
    _clear_lease(db)
    assert holds_master_lease(db) is False
    db.close()


def test_next_task_refused_without_lease(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    clear_master_lease(db)
    code, data = http_request(HandlerClass, "GET", "/api/worker/next-task?worker_id=w1")
    assert code == 503
    assert data["error"] == "not master"
    assert data["task"] is None
    db.close()


def test_next_task_allowed_with_lease(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, projects=[], session_id=None)
    code, data = http_request(HandlerClass, "GET", "/api/worker/next-task?worker_id=w1")
    assert code == 200
    assert data is not None
    assert "task" in data
    db.close()


def test_yield_master_then_next_task_refused(tmp_path: Path) -> None:
    """T-033: after /api/yield-master this HTTP process cannot assign."""
    HandlerClass, db, _ = make_handler_env(tmp_path)

    code, data = http_request(HandlerClass, "POST", "/api/yield-master")
    assert code == 200
    assert data.get("ok") is True

    code, data = http_request(HandlerClass, "GET", "/api/worker/next-task?worker_id=w1")
    assert code == 503
    assert data.get("error") == "not master"
    assert data.get("task") is None
    db.close()


def test_finalize_and_register_refused_without_lease(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    clear_master_lease(db)

    code, _ = http_request(
        HandlerClass,
        "POST",
        "/api/worker/finalize",
        {"run_id": 1, "worker_id": "w1", "status": "completed", "exit_code": 0},
    )
    assert code == 503

    code2, _ = http_request(
        HandlerClass,
        "POST",
        "/api/worker/register",
        {"worker_id": "w1", "os": "windows", "arch": "amd64", "ram_gib": 8, "cpu_cores": 4},
    )
    assert code2 == 503

    code3, _ = http_request(
        HandlerClass,
        "POST",
        "/api/worker/heartbeat",
        {"worker_id": "w1"},
    )
    assert code3 == 503
    db.close()
