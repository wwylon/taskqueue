"""P7 / R-007: long-poll GET /api/worker/next-task?wait="""

from __future__ import annotations

import time
from pathlib import Path

from taskqueue.config import WORK_LONGPOLL_MAX_SEC
from tests.helpers.master_worker import http_request, make_handler_env, seed_worker


def test_wait_zero_is_immediate(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, projects=[], session_id=None)
    t0 = time.monotonic()
    code, data = http_request(
        HandlerClass, "GET", "/api/worker/next-task?worker_id=w1&wait=0"
    )
    elapsed = time.monotonic() - t0
    assert code == 200
    assert data["task"] is None
    assert elapsed < 2.0  # wait=0 must not long-poll; allow slow CI hosts
    db.close()


def test_wait_holds_until_deadline_when_idle(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, projects=[], session_id=None)
    t0 = time.monotonic()
    code, data = http_request(
        HandlerClass, "GET", "/api/worker/next-task?worker_id=w1&wait=0.5"
    )
    elapsed = time.monotonic() - t0
    assert code == 200
    assert data["task"] is None
    assert elapsed >= 0.45
    assert elapsed < 2.0
    db.close()


def test_longpoll_max_is_capped() -> None:
    assert WORK_LONGPOLL_MAX_SEC == 30
