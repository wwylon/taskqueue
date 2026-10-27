"""Queue health verdict: taskqueue.health, `tq health`, /api/health, coloured toasts and CLI colours."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskqueue import config as tqconfig
from taskqueue._fmt import color_status
from taskqueue.db import open_db
from taskqueue.health import compute_health, format_line, read_ack, write_ack
from tests.helpers.master_worker import http_request, make_handler_env


def _iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _task(conn, task_id: str, status: str, *, ended_min_ago: float = 5.0, enabled: int = 1,
          error_code: str | None = None, project: str = "p") -> None:
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enabled, last_error_code, enqueued_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (project, task_id, "python", status, enabled, error_code, _iso(60), _iso(0)),
    )
    cur = conn.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status) VALUES (?,?,?,?,?)",
        (project, task_id, _iso(ended_min_ago + 1), _iso(ended_min_ago), status),
    )
    conn.execute(
        "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?",
        (cur.lastrowid, project, task_id),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    c = open_db(tqconfig.db_path(tmp_path))
    yield c
    c.close()


def test_green_when_nothing_needs_a_human(conn) -> None:
    _task(conn, "ok", "completed")
    _task(conn, "run", "running")
    _task(conn, "shelved", "failed", enabled=0)  # disabled = parked on purpose
    h = compute_health(conn, daemon_up=True)
    assert h["level"] == "green" and h["summary"] == "ok" and h["parts"] == []
    assert h["counts"]["running"] == 1


def test_new_failure_is_red_and_ack_turns_it_amber(conn, tmp_path: Path) -> None:
    _task(conn, "boom", "failed", ended_min_ago=10)
    assert compute_health(conn)["level"] == "red"

    ack = write_ack(tmp_path, "alice")
    assert read_ack(tmp_path) == ack
    h = compute_health(conn, ack_at=ack)
    assert h["level"] == "amber"
    assert [p["kind"] for p in h["parts"]] == ["failed"]

    _task(conn, "later", "unknown", ended_min_ago=-1)  # ended after the ack
    h = compute_health(conn, ack_at=ack)
    assert h["level"] == "red" and h["new_failed"] == ["p/later"]
    assert h["counts"] == {**h["counts"], "new_failed": 1, "failed": 2}


def test_frozen_interrupted_and_daemon_down(conn) -> None:
    _task(conn, "cap", "interrupted")
    _task(conn, "stuck", "running", error_code="tq.run.frozen")
    h = compute_health(conn, daemon_up=True)
    assert h["level"] == "amber" and h["frozen"] == ["p/stuck"] and h["interrupted"] == ["p/cap"]
    assert compute_health(conn, daemon_up=False)["level"] == "red"


def test_format_line_icons_and_plain(conn) -> None:
    _task(conn, "boom", "failed")
    _task(conn, "run", "running")
    h = compute_health(conn, daemon_up=False)
    line = format_line(h)
    assert line.startswith("\U0001F534 tq") and "\U0001F480" in line and "❌1" in line
    assert "\U0001F4801" not in line  # daemon-down icon carries no count
    plain = format_line(h, icons=False)
    assert plain.startswith("[!!] tq daemon down, 1 new failed") and "1 running" in plain
    assert "\033[" in format_line(h, icons=False, color=True)


def test_tq_health_cli_exit_codes(conn, tmp_path: Path, capsys, monkeypatch) -> None:
    from taskqueue import cli
    from taskqueue.cli import _cmd_health

    monkeypatch.setattr(cli, "_daemon_running", lambda _home: 4242)
    _task(conn, "boom", "failed")
    assert _cmd_health(tmp_path, ["--plain"]) == 2
    assert "New failures: p/boom" in capsys.readouterr().out
    assert _cmd_health(tmp_path, ["--line", "--plain"]) == 0  # status lines never fail
    assert _cmd_health(tmp_path, ["--ack", "--plain"]) == 1
    assert _cmd_health(tmp_path, ["--bogus"]) == 3


def test_api_health_and_ack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    Handler, db, home = make_handler_env(tmp_path)
    try:
        _task(db, "boom", "failed")
        code, body = http_request(Handler, "GET", "/api/health")
        assert code == 200 and body["level"] == "red" and body["daemon_up"] is True
        code, status = http_request(Handler, "GET", "/api/status")
        assert status["health"]["level"] == "red"
        code, body = http_request(Handler, "POST", "/api/health/ack", {})
        assert code == 200 and body["ok"] and body["health"]["level"] == "amber"
        assert read_ack(home) == body["ack_at"]
        audit = db.execute("SELECT action FROM operator_audit WHERE action = 'health_ack'").fetchall()
        assert len(audit) == 1
    finally:
        db.close()


def test_toast_level_prefix_and_icon(monkeypatch) -> None:
    from taskqueue import notify

    sent: dict = {}

    class _N:
        def __init__(self, **kw):
            sent.update(kw)

        def set_audio(self, *a, **k):
            pass

        def show(self):
            pass

    import sys
    import types

    fake = types.SimpleNamespace(Notification=lambda **kw: _N(**kw), audio=types.SimpleNamespace(Default=None))
    monkeypatch.setitem(sys.modules, "winotify", fake)
    notify._send_toast("Task failed: p/a", "body", level="red")
    assert sent["title"].startswith("\U0001F534 ")
    icon = Path(sent["icon"])
    assert icon.is_file() and icon.read_bytes().startswith(b"\x89PNG")

    from taskqueue.system_alerts import SYSTEM_ALERT_KEYS, SYSTEM_ALERT_LEVELS

    assert set(SYSTEM_ALERT_LEVELS) == set(SYSTEM_ALERT_KEYS)


def test_color_status() -> None:
    assert color_status("failed  ", "failed", enabled=True) == "\033[31mfailed  \033[0m"
    assert color_status("queued", "queued", enabled=True) == "queued"
    assert color_status("failed", "failed", enabled=False) == "failed"
