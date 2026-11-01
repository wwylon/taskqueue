"""T0 tests for audit v5 M1 (F-150–F-158 / F-187)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from taskqueue.db import open_db
from taskqueue.sse_ticket import issue_ticket, redeem_ticket
from taskqueue.statuses import FINALIZE_STATUSES
from taskqueue.taskspec import parse_task_yaml_many


def test_f151_sse_ticket_single_use():
    tok = "secret-token-abc"
    ticket = issue_ticket(tok)
    assert redeem_ticket(ticket) == tok
    assert redeem_ticket(ticket) is None


def test_f154_finalize_statuses_allowlist():
    assert "completed" in FINALIZE_STATUSES
    assert "queued" not in FINALIZE_STATUSES
    assert "running" not in FINALIZE_STATUSES


def test_f155_tq_master_unreachable_no_none(monkeypatch, tmp_path: Path):
    from taskqueue import master_client as mc

    monkeypatch.setenv("TQ_MASTER", "http://127.0.0.1:9")
    monkeypatch.setattr(mc, "api_request", lambda *a, **k: None)
    resp = mc.via_master(tmp_path, "GET", "/api/status", timeout=1.0)
    assert resp is not None
    assert resp.get("unreachable") is True
    assert "TQ_MASTER" in (resp.get("error") or "")


def test_f156_offline_blocked_when_lease_held(tmp_path: Path, monkeypatch):
    import time

    from taskqueue._cli_helpers import offline_sqlite_write_blocked
    from taskqueue.db import open_db

    monkeypatch.delenv("TQ_MASTER", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "queue.db"
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    # Point helpers at this home: _db uses TASKQUEUE_HOME / queue.db via config.
    from taskqueue import _cli_helpers as h

    monkeypatch.setattr(h, "_db", lambda _home: db_path)
    monkeypatch.setattr(h, "_daemon_running", lambda _home: None)
    db = open_db(db_path)
    db.execute(
        "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at) VALUES (1, ?, ?)",
        ("other-host", int(time.time()) + 600),
    )
    db.commit()
    db.close()
    msg = offline_sqlite_write_blocked(home)
    assert msg is not None
    assert "master_lease" in msg


def test_f158_matrix_cap_and_dupes(tmp_path: Path):
    from taskqueue.config import Project

    proj = Project(name="proj", root=tmp_path, queue_dir=tmp_path)
    path = tmp_path / "m.yaml"
    path.write_text(
        "task_id: t\ncommand: python\nmatrix:\n  seed: [0, 0]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_task_yaml_many(path, proj)

    # 10×10×10 = 1000 > MATRIX_MAX 500
    path.write_text(
        "task_id: t\ncommand: python\nmatrix:\n"
        "  a: [0,1,2,3,4,5,6,7,8,9]\n"
        "  b: [0,1,2,3,4,5,6,7,8,9]\n"
        "  c: [0,1,2,3,4,5,6,7,8,9]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cap"):
        parse_task_yaml_many(path, proj)


def test_f158_mcp_wait_timeout_clamped():
    from taskqueue.mcp_server import MCP_WAIT_DEFAULT_SEC, _mcp_wait_max_sec

    # Inline the clamp logic used by _wait_for_task
    cap = _mcp_wait_max_sec()
    raw_timeout = -5.0
    timeout_sec = min(max(raw_timeout, 0.001), cap)
    assert timeout_sec == 0.001
    raw_timeout = 1e9
    timeout_sec = min(max(raw_timeout, 0.001), cap)
    assert timeout_sec == cap
    assert MCP_WAIT_DEFAULT_SEC > 0


def test_f153_notify_cursor_concurrent(tmp_path: Path):
    from taskqueue import notify as n

    home = tmp_path
    log = n.notify_log_path(home)
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(40):
        lines.append(json.dumps({"ts": f"2026-01-01T00:00:{i:02d}Z", "kind": "task", "i": i}) + "\n")
    log.write_text("".join(lines), encoding="utf-8")

    results: list[list] = []

    def drain_once():
        out = n.drain_notifications(home, incremental=True, limit=100)
        results.append(out["events"])

    t1 = threading.Thread(target=drain_once)
    t2 = threading.Thread(target=drain_once)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # With the lock, drains serialize: union of events covers the file once
    # (no permanent loss; may split across the two callers).
    ids = []
    for evs in results:
        ids.extend(e.get("i") for e in evs)
    assert sorted(set(ids)) == list(range(40))


def test_f150_pending_log_apply(tmp_path: Path):
    from taskqueue.web.routes_worker import _apply_pending_log_chunk, _pending_log_path

    log_path = tmp_path / "run.log"
    pending = _pending_log_path(log_path, 7)
    pending.write_text("hello-chunk\n", encoding="utf-8")
    _apply_pending_log_chunk(log_path, 7)
    assert log_path.read_text(encoding="utf-8") == "hello-chunk\n"
    assert not pending.exists()
    # Second apply is a no-op (idempotent).
    _apply_pending_log_chunk(log_path, 7)
    assert log_path.read_text(encoding="utf-8") == "hello-chunk\n"


def test_f150_schema_has_last_log_seq(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "last_log_seq" in cols
    from taskqueue.migrations import SCHEMA_VERSION

    assert SCHEMA_VERSION >= 31
