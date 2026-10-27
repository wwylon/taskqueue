"""System alerts: seed, toggles, emit conditions, toast title."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from http.client import parse_headers
from pathlib import Path
from unittest.mock import patch

from taskqueue.db import open_db
from taskqueue.system_alerts import (
    SYSTEM_ALERT_KEYS,
    evaluate_all_done,
    format_tq_stop_why,
    list_system_alerts,
    maybe_emit_system_alert,
    on_run_terminal,
    queue_status_triplet,
    toast_title_from_counts,
    update_system_alert,
)
from taskqueue.web import make_handler


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def test_seed_system_alerts(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    rows = db.execute("SELECT key FROM system_alerts ORDER BY key").fetchall()
    assert [r["key"] for r in rows] == sorted(SYSTEM_ALERT_KEYS)


def test_toast_title_format():
    assert toast_title_from_counts(2, 1, 5) == "tq 2/1/5"


def test_format_tq_stop_why():
    assert "RAM" in format_tq_stop_why("ram_hard", "tq.stop.ram")
    assert format_tq_stop_why(None, None) == "stopped by TaskQueue"


def test_update_system_alert_toggles(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    out = update_system_alert(db, "any_failure", {"enabled": 0, "windows_toast": 0})
    assert out is not None
    assert out["enabled"] == 0
    assert out["windows_toast"] == 0
    assert update_system_alert(db, "nope", {"enabled": 1}) is None


def test_on_run_terminal_any_failure(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "any_failure", {"windows_toast": 0})
    with patch("taskqueue.notify._send_toast") as toast:
        on_run_terminal(
            db, tmp_path,
            project="p", task_id="t1", status="failed",
            last_error_code="tq.run.nonzero",
        )
        toast.assert_not_called()
    row = db.execute(
        "SELECT fire_count FROM system_alerts WHERE key='any_failure'"
    ).fetchone()
    assert int(row["fire_count"]) == 1


def test_on_run_terminal_tq_stopped_not_user(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "tq_stopped", {"windows_toast": 0})
    on_run_terminal(
        db, tmp_path,
        project="p", task_id="t1", status="stopped",
        stopped_by="user", stop_reason="stop_requested",
    )
    assert db.execute(
        "SELECT fire_count FROM system_alerts WHERE key='tq_stopped'"
    ).fetchone()["fire_count"] == 0
    on_run_terminal(
        db, tmp_path,
        project="p", task_id="t1", status="stopped",
        stopped_by="tq", stop_reason="ram_hard", last_error_code="tq.stop.ram",
    )
    assert db.execute(
        "SELECT fire_count FROM system_alerts WHERE key='tq_stopped'"
    ).fetchone()["fire_count"] == 1


def test_all_done_requires_idle_and_recent(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "all_done", {"windows_toast": 0})
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert evaluate_all_done(db, tmp_path, now=now) is False

    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, priority, enabled, "
        "enqueued_at, updated_at) VALUES ('p','t1','completed','true',0,1,?,?)",
        (_iso(now), _iso(now)),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at) "
        "VALUES ('p','t1','completed',?,?)",
        (_iso(now - timedelta(minutes=5)), _iso(now - timedelta(minutes=1))),
    )
    db.commit()
    assert evaluate_all_done(db, tmp_path, now=now) is True
    assert evaluate_all_done(db, tmp_path, now=now + timedelta(minutes=1)) is False

    db.execute("UPDATE tasks SET status='queued' WHERE project='p' AND task_id='t1'")
    db.commit()
    assert evaluate_all_done(db, tmp_path, now=now + timedelta(hours=2)) is False


def test_windows_toast_uses_triplet_title(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, priority, enabled, "
        "enqueued_at, updated_at) VALUES "
        "('p','a','running','true',0,1,'t','t'),"
        "('p','b','failed','true',0,1,'t','t'),"
        "('p','c','queued','true',0,1,'t','t'),"
        "('p','d','queued','true',0,1,'t','t')"
    )
    db.commit()
    assert queue_status_triplet(db) == (1, 1, 2)
    with patch("taskqueue.notify._send_toast") as toast:
        maybe_emit_system_alert(db, tmp_path, "any_failure", "body")
        toast.assert_called_once()
        assert toast.call_args[0][0] == "tq 1/1/2"
        assert toast.call_args[0][1] == "body"


def test_disabled_alert_does_not_emit(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "any_failure", {"enabled": 0})
    assert maybe_emit_system_alert(db, tmp_path, "any_failure", "x") is False


def test_emit_appends_notify_jsonl_without_winotify(tmp_path: Path):
    """Emit updates fire_count and notify.jsonl even when toast cannot show."""
    from taskqueue.notify import notify_log_path

    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "any_failure", {"windows_toast": 1})
    with patch("taskqueue.notify._send_toast") as toast:
        assert maybe_emit_system_alert(db, tmp_path, "any_failure", "p/t failed") is True
        toast.assert_called_once()
    row = db.execute(
        "SELECT fire_count, last_fired_at FROM system_alerts WHERE key='any_failure'"
    ).fetchone()
    assert int(row["fire_count"]) == 1
    assert row["last_fired_at"]
    path = notify_log_path(tmp_path)
    assert path.is_file()
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1
    assert lines[0]["status"] == "alert:any_failure"
    assert lines[0]["description"] == "p/t failed"
    assert lines[0]["started_by"] == "alert"


def test_send_toast_logs_missing_winotify_once(caplog):
    import logging

    import taskqueue.notify as notify_mod

    notify_mod._winotify_missing_logged = False
    notify_mod._toast_fail_logged = False
    with (
        patch.dict("sys.modules", {"winotify": None}),
        patch("builtins.__import__", side_effect=ImportError("no winotify")),
        caplog.at_level(logging.WARNING, logger="taskqueue.notify"),
    ):
        # Force ImportError inside _send_toast's from winotify import
        real_import = __import__

        def _imp(name, *a, **k):
            if name == "winotify" or name.startswith("winotify."):
                raise ImportError("no winotify")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=_imp):
            notify_mod._send_toast("tq 0/0/0", "body")
            notify_mod._send_toast("tq 0/0/0", "body2")
    msgs = [r.message for r in caplog.records if "winotify not installed" in r.message]
    assert len(msgs) == 1


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


def _make_request(HandlerClass, method: str, path: str, body: bytes = b"",
                  extra_headers: dict | None = None):
    headers_raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:8765\r\n"
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


def test_api_system_alerts_get_patch(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, data = _make_request(HandlerClass, "GET", "/api/alerts/system")
    assert code == 200
    payload = json.loads(data.decode())
    assert len(payload["alerts"]) == len(SYSTEM_ALERT_KEYS) == 5
    keys = {a["key"] for a in payload["alerts"]}
    assert keys == set(SYSTEM_ALERT_KEYS)

    code, data = _make_request(
        HandlerClass, "PATCH", "/api/alerts/system/all_done",
        body=json.dumps({"enabled": 0, "windows_toast": 0}).encode(),
    )
    assert code == 200
    out = json.loads(data.decode())
    assert out["ok"] is True
    assert out["alert"]["enabled"] == 0

    code, data = _make_request(
        HandlerClass, "PATCH", "/api/alerts/system/missing",
        body=json.dumps({"enabled": 1}).encode(),
    )
    assert code == 404

    listed = list_system_alerts(db)
    all_done = next(a for a in listed["alerts"] if a["key"] == "all_done")
    assert all_done["enabled"] == 0


def _events(home: Path):
    from taskqueue.notify import iter_notify_events, notify_log_path

    return iter_notify_events(notify_log_path(home), limit=50)


def test_failure_alert_carries_detail_and_log_tail(tmp_path: Path, capsys):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "any_failure", {"windows_toast": 0})
    log = tmp_path / "t1.log"
    log.write_text("starting\nloading data\nTraceback: boom\n", encoding="utf-8")
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path) VALUES (?,?,?,?,?)",
        ("p", "t1", "2026-09-01T00:00:00+00:00", "failed", str(log)),
    )
    rid = db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    db.commit()
    on_run_terminal(
        db, tmp_path, project="p", task_id="t1", status="failed", run_id=rid,
        last_error_code="tq.run.nonzero", exit_code=2, elapsed_sec=75,
    )
    ev = [e for e in _events(tmp_path) if e["status"] == "alert:any_failure"][-1]
    d = ev["detail"]
    assert d["title"] == "Task failed: p/t1"
    assert d["exit_code"] == 2 and d["run_id"] == rid
    assert d["error_label"] == "non-zero exit code"
    assert d["log_tail"][-1] == "Traceback: boom"
    assert "Traceback: boom" in ev["description"]
    assert "[alert]" in capsys.readouterr().out


def test_toast_uses_event_title_and_triplet_in_body(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "any_failure", {"windows_toast": 1})
    with patch("taskqueue.notify._send_toast") as toast:
        on_run_terminal(db, tmp_path, project="p", task_id="t1", status="failed", exit_code=1)
        toast.assert_called_once()
        title, body = toast.call_args[0][:2]
    assert title == "Task failed: p/t1"
    assert "p/t1 failed" in body and "running" in body and "failed" in body


def test_unknown_error_code_uses_registry_description(tmp_path: Path):
    from taskqueue.system_alerts import error_label

    assert error_label("tq.stop.scavenger") != "tq.stop.scavenger"
    assert error_label("tq.nope.unknown") == "tq.nope.unknown"
    assert error_label(None) is None


def test_all_done_detail_lists_failed(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    update_system_alert(db, "all_done", {"windows_toast": 0})
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("p", "bad", "x", "failed", _iso(now), _iso(now)),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status) VALUES (?,?,?,?,?)",
        ("p", "bad", _iso(now - timedelta(minutes=10)), _iso(now - timedelta(minutes=5)), "failed"),
    )
    db.commit()
    assert evaluate_all_done(db, tmp_path, now=now) is True
    ev = [e for e in _events(tmp_path) if e["status"] == "alert:all_done"][-1]
    assert ev["detail"]["failed_tasks"] == ["p/bad"]
    assert "failed: p/bad" in ev["description"]
