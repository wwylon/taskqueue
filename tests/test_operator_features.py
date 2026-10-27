"""Percentiles, timeline window, cron next-fire, alert cooldown, audit, tokens."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta, timezone
from http.client import parse_headers
from pathlib import Path

from taskqueue.db import open_db, set_config, set_project_limit
from taskqueue.operator_features import (
    create_alert,
    evaluate_alerts,
    next_cron_fire,
    task_stats,
    timeline,
    update_alert,
)
from taskqueue.timeline_forecast import (
    build_duration_index,
    estimate_duration_sec,
    tasks_similar,
)
from taskqueue.web import make_handler


def _make_handler(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    return make_handler(db, {}, {}, lambda: {}, tmp_path / "q.db", "127.0.0.1"), db


class _FakeWfile:
    def __init__(self):
        self.data = b""

    def write(self, d):
        self.data += d

    def flush(self):
        pass


def _make_request(HandlerClass, method: str, path: str, body: bytes = b"", extra_headers: dict | None = None):
    headers_raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:8766\r\n"
        f"Content-Length: {len(body)}\r\n"
    )
    for k, v in (extra_headers or {}).items():
        headers_raw += f"{k}: {v}\r\n"
    headers_raw += "\r\n"
    raw = headers_raw.encode() + body
    wfile = _FakeWfile()
    status = []

    class TestHandler(HandlerClass):
        def setup(self):
            self.rfile = io.BufferedReader(io.BytesIO(raw))
            self.wfile = wfile
            self.raw_requestline = f"{method} {path} HTTP/1.1\r\n".encode()
            self.requestline = self.raw_requestline.decode()
            self.request_version = "HTTP/1.1"
            self.command = method
            self.path = path
            self.headers = parse_headers(io.BytesIO(headers_raw.encode().split(b"\r\n", 1)[1]))
            self.connection = "close"
            self.close_connection = True
            self.timeout = 5

        def send_response(self, code, msg=None):
            status.append(code)

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

        def log_message(self, *a):
            pass

    h = TestHandler.__new__(TestHandler)
    TestHandler.setup(h)
    h.handle_one_request()
    return status[0] if status else None, wfile.data


def _insert_run(db, project, task_id, started, finished, status="completed"):
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status) VALUES (?, ?, ?, ?, ?)",
        (project, task_id, started, finished, status),
    )
    db.commit()


def _mk_task(
    db,
    project: str,
    task_id: str,
    *,
    status: str = "queued",
    priority: int = 0,
    depends_on: list | None = None,
    schedule: dict | None = None,
    enabled: int = 1,
    enqueued_at: str | None = None,
):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, priority, enqueued_at, updated_at, "
        "enabled, depends_on, schedule) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            project,
            task_id,
            "python",
            status,
            priority,
            enqueued_at or now,
            now,
            enabled,
            json.dumps(depends_on or []),
            json.dumps(schedule) if schedule is not None else None,
        ),
    )
    db.commit()


def test_task_stats_percentiles(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for i, seconds in enumerate((10, 20, 30, 40)):
        start = base + timedelta(hours=i)
        _insert_run(
            db, "proj", "train",
            start.isoformat(),
            (start + timedelta(seconds=seconds)).isoformat(),
            "completed" if i < 3 else "failed",
        )
    rows = task_stats(db, project="proj")
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 4
    # mean/p50/p95 from completed {10,20,30} only — failed 40 excluded
    assert row["mean_sec"] == 20.0
    assert row["p50_sec"] == 20.0
    assert row["p95_sec"] == 30.0
    assert row["success_rate"] == 0.75


def test_task_stats_null_when_no_completed(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _insert_run(
        db, "proj", "fail_only",
        base.isoformat(),
        (base + timedelta(seconds=15)).isoformat(),
        "failed",
    )
    rows = task_stats(db, project="proj", task_id="fail_only")
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 1
    assert row["mean_sec"] is None
    assert row["p50_sec"] is None
    assert row["p95_sec"] is None
    assert row["success_rate"] == 0.0


def test_duration_cascade_own_history(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for sec in (100, 200, 300):
        _insert_run(db, "proj", "job_a", base.isoformat(), (base + timedelta(seconds=sec)).isoformat())
    _insert_run(db, "proj", "other", base.isoformat(), (base + timedelta(seconds=50)).isoformat())
    idx = build_duration_index(db)
    dur, src = estimate_duration_sec("proj", "job_a", idx)
    assert src == "history"
    assert dur == 200.0


def test_duration_cascade_similar_then_global(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _insert_run(db, "proj", "belote_seed0", base.isoformat(), (base + timedelta(seconds=400)).isoformat())
    _insert_run(db, "proj", "belote_seed1", base.isoformat(), (base + timedelta(seconds=600)).isoformat())
    _insert_run(db, "other", "unrelated", base.isoformat(), (base + timedelta(seconds=50)).isoformat())
    idx = build_duration_index(db)
    assert tasks_similar("belote_seed2", "belote_seed0")
    dur, src = estimate_duration_sec("proj", "belote_seed2", idx)
    assert src == "similar"
    assert dur == 500.0

    dur2, src2 = estimate_duration_sec("lonely", "brand_new", idx)
    assert src2 == "global_avg"
    assert dur2 == (400 + 600 + 50) / 3


def test_timeline_serial_under_max_tasks(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    base = now - timedelta(days=1)
    for tid, sec in (("a", 100), ("b", 100)):
        _insert_run(db, "proj", tid, base.isoformat(), (base + timedelta(seconds=sec)).isoformat())
        _mk_task(db, "proj", tid, enqueued_at=(now - timedelta(minutes=5 if tid == "a" else 1)).isoformat())
    set_project_limit(db, "proj", "max_tasks", 1)
    payload = timeline(db, hours=6, now=now, project="proj")
    bars = {b["task_id"]: b for b in payload["bars"]}
    assert set(bars) == {"a", "b"}
    assert bars["a"]["order"] == 1
    assert bars["b"]["order"] == 2
    assert bars["a"]["start_sec"] <= bars["b"]["start_sec"]
    assert bars["a"]["start_sec"] == 0.0
    assert bars["b"]["start_sec"] == bars["a"]["end_sec"]
    assert abs(payload["bunch_end_sec"] - 200.0) < 1.0


def test_timeline_parallel_when_max_tasks_two(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    base = now - timedelta(days=1)
    for tid in ("a", "b"):
        _insert_run(db, "proj", tid, base.isoformat(), (base + timedelta(seconds=100)).isoformat())
        _mk_task(db, "proj", tid)
    set_project_limit(db, "proj", "max_tasks", 2)
    payload = timeline(db, hours=6, now=now, project="proj")
    bars = payload["bars"]
    assert len(bars) == 2
    assert bars[0]["start_sec"] == 0.0
    assert bars[1]["start_sec"] == 0.0
    assert abs(payload["bunch_end_sec"] - 100.0) < 1.0


def test_timeline_depends_on_delays_child(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    base = now - timedelta(days=1)
    for tid, sec in (("parent", 120), ("child", 60)):
        _insert_run(db, "proj", tid, base.isoformat(), (base + timedelta(seconds=sec)).isoformat())
    _mk_task(db, "proj", "parent", priority=10)
    _mk_task(db, "proj", "child", priority=0, depends_on=["parent"])
    set_project_limit(db, "proj", "max_tasks", 2)
    payload = timeline(db, hours=6, now=now, project="proj")
    bars = {b["task_id"]: b for b in payload["bars"]}
    assert bars["child"]["start_sec"] == bars["parent"]["end_sec"]


def test_timeline_cron_arrival(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    base = now - timedelta(days=1)
    _insert_run(db, "proj", "nightly", base.isoformat(), (base + timedelta(seconds=30)).isoformat())
    _mk_task(
        db, "proj", "nightly",
        status="completed",
        schedule={"cron": "0 0 * * *"},
    )
    payload = timeline(db, hours=24, now=now, project="proj")
    bars = [b for b in payload["bars"] if b["task_id"] == "nightly"]
    assert len(bars) == 1
    assert bars[0]["kind"] == "cron"
    assert abs(bars[0]["start_sec"] - 12 * 3600) < 60


def test_next_cron_fire_within_48h():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    nxt = next_cron_fire("0 0 * * *", now)
    assert nxt == datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def test_alert_cooldown_skips_second_fire(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    _insert_run(
        db, "proj", "job",
        (now - timedelta(minutes=5)).isoformat(),
        now.isoformat(),
        "failed",
    )
    create_alert(db, {
        "name": "fails",
        "kind": "failed_count",
        "threshold": 0,
        "window_sec": 3600,
        "action": "notify",
    })
    cfg = {"toast_notifications": False}
    first = evaluate_alerts(db, tmp_path, None, cfg, now=now)
    second = evaluate_alerts(db, tmp_path, None, cfg, now=now + timedelta(minutes=1))
    assert len(first) == 1
    assert second == []
    assert db.execute("SELECT fire_count FROM alert_rules").fetchone()["fire_count"] == 1


def test_update_alert_enabled_false_is_case_insensitive(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    rule = create_alert(db, {"name": "r", "kind": "failed_count", "threshold": 1})
    assert update_alert(db, rule["id"], {"enabled": "False"})["enabled"] == 0
    assert update_alert(db, rule["id"], {"enabled": "True"})["enabled"] == 1


def test_stop_writes_audit_row(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    stamp = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES ('proj', 'T1', 'queued', 'true', ?, ?, 1)",
        (stamp, stamp),
    )
    db.commit()
    code, _ = _make_request(
        HandlerClass, "POST", "/api/stop/proj/T1",
        extra_headers={"X-TQ-Operator": "ada"},
    )
    assert code == 200
    code, body = _make_request(HandlerClass, "GET", "/api/audit?limit=10")
    assert code == 200
    events = json.loads(body)["events"]
    assert events[0]["action"] == "stop"
    assert events[0]["actor"] == "ada"
    assert events[0]["project"] == "proj"
    assert events[0]["target"] == "T1"


def test_token_hash_revoke_and_scope(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    set_config(db, "auth_token", "legacy-secret")
    code, body = _make_request(
        HandlerClass, "POST", "/api/tokens",
        body=json.dumps({"name": "reader", "scope": "read"}).encode(),
        extra_headers={"Authorization": "Bearer legacy-secret"},
    )
    assert code == 200
    created = json.loads(body)
    secret = created["token"]
    assert secret.startswith("tq_live_")
    stored = db.execute("SELECT token_hash, prefix FROM api_tokens WHERE id = ?", (created["id"],)).fetchone()
    assert stored["token_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert stored["token_hash"] != secret
    audit = db.execute(
        "SELECT detail FROM operator_audit WHERE action = 'token_create'"
    ).fetchone()
    assert secret not in (audit["detail"] or "")

    code, _ = _make_request(
        HandlerClass, "GET", "/api/task-stats",
        extra_headers={"Authorization": f"Bearer {secret}"},
    )
    assert code == 200
    code, body = _make_request(
        HandlerClass, "POST", "/api/stop/proj/missing",
        extra_headers={"Authorization": f"Bearer {secret}"},
    )
    assert code == 403
    assert b"scope" in body

    code, _ = _make_request(
        HandlerClass, "DELETE", f"/api/tokens/{created['id']}",
        extra_headers={"Authorization": "Bearer legacy-secret"},
    )
    assert code == 200
    code, _ = _make_request(
        HandlerClass, "GET", "/api/task-stats",
        extra_headers={"Authorization": f"Bearer {secret}"},
    )
    assert code == 401
