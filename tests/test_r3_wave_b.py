"""Round 3 Wave B — F-064 CSRF on next-task, F-068 _read_json fail-closed."""

from __future__ import annotations

import io
import json
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.web import make_handler
from tests.helpers.master_worker import http_request, make_handler_env, seed_master_lease


def test_next_task_rejects_foreign_origin(tmp_path: Path) -> None:
    """F-064: GET /api/worker/next-task is state-changing and must honor CSRF Origin."""
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_master_lease(db)
    code, data = http_request(
        HandlerClass,
        "GET",
        "/api/worker/next-task?worker_id=w1",
        extra_headers={"Origin": "https://evil.example"},
    )
    assert code == 403
    assert isinstance(data, dict) and "Cross-origin" in str(data.get("error", ""))
    db.close()


def test_next_task_rejects_cross_site_fetch_metadata(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_master_lease(db)
    code, data = http_request(
        HandlerClass,
        "GET",
        "/api/worker/next-task?worker_id=w1",
        extra_headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert code == 403
    db.close()


def test_read_json_oversized_returns_none(tmp_path: Path) -> None:
    """F-068: oversized body sends 413 and returns None (not {})."""
    db = open_db(tmp_path / "q.db")
    HandlerClass = make_handler(db, {}, {}, lambda: {}, tmp_path / "q.db")
    big = b"x" * (1024 * 1024 + 1)
    status_codes: list[int] = []

    class Stub(HandlerClass):
        def setup(self):
            self.headers = {"Content-Length": str(len(big))}
            self.rfile = io.BytesIO(big)
            self.wfile = io.BytesIO()
            self.command = "POST"
            self.path = "/api/config"

        def send_response(self, code, msg=None):
            status_codes.append(code)

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    s = Stub.__new__(Stub)
    Stub.setup(s)
    result = s._read_json()
    assert result is None
    assert 413 in status_codes
    db.close()


def test_post_config_rejects_oversized_body(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_master_lease(db)
    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/config",
        b"{" + b"x" * (1024 * 1024) + b"}",
        extra_headers={"Content-Type": "application/json", "Content-Length": str(1024 * 1024 + 2)},
    )
    assert code == 413
    db.close()
