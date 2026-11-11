"""ETA for agents: time left while running, and how much was left when a run was stopped."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from tests.helpers.master_worker import http_request, make_handler_env
from tqdaemon._db_tasks import upsert_task
from tqdaemon.config import Project
from tqdaemon.db import open_db, parse_task_yaml
from tqdaemon.eta import build_eta, remaining, task_eta
from tqdaemon.metrics_snapshot import build_run_metrics
from tqdaemon.waiting import wait_for

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_remaining_prefers_rate_then_average_then_history() -> None:
    assert remaining(progress_pct=40, timing={"rate": 0.01}, elapsed_sec=600, predicted_sec=None) == (6000.0, "rate")
    assert remaining(progress_pct=40, timing=None, elapsed_sec=600, predicted_sec=None) == (900.0, "average")
    assert remaining(progress_pct=None, timing=None, elapsed_sec=600, predicted_sec=1000) == (400.0, "history")
    assert remaining(progress_pct=None, timing=None, elapsed_sec=1500, predicted_sec=1000) == (0.0, "history")
    assert remaining(progress_pct=100, timing={"rate": 0.01}, elapsed_sec=600, predicted_sec=1000) is None
    assert remaining(progress_pct=None, timing=None, elapsed_sec=600, predicted_sec=None) is None


def test_running_eta_has_finish_time() -> None:
    eta = build_eta(
        task_status="running",
        run_status="running",
        progress_pct=50,
        metrics_json=json.dumps({"_progress": {"rate": 0.05}}),
        started_at=_iso(NOW - timedelta(minutes=10)),
        finished_at=None,
        wall_sec=None,
        predicted_sec=None,
        now=NOW.timestamp(),
    )
    assert eta is not None
    assert eta["remaining_sec"] == 1000 and eta["basis"] == "rate" and "at_stop" not in eta
    assert eta["finish_at"] == _iso(NOW + timedelta(seconds=1000))
    assert eta["text"] == "~16m40s left (rate)"


def test_stopped_run_keeps_the_eta_it_had_when_stopped() -> None:
    kw = dict(
        task_status="stopped",
        run_status="stopped",
        progress_pct=25,
        metrics_json={"_progress": {"rate": 0.01}},
        started_at=_iso(NOW - timedelta(hours=2)),
        finished_at=_iso(NOW - timedelta(hours=1)),
        wall_sec=3600,
        predicted_sec=5000,
    )
    a = build_eta(**kw, now=NOW.timestamp())
    b = build_eta(**kw, now=NOW.timestamp() + 86400)  # a day later: same answer, the clock stopped with the run
    assert a == b
    assert a["remaining_sec"] == 7500 and a["at_stop"] is True and a["stopped_at"] == kw["finished_at"]
    assert "finish_at" not in a and a["typical_sec"] == 5000
    assert a["text"] == "was ~2h05m00s from done at 25% when the run ended (stopped, rate)"
    # A completed last run has nothing left; only the typical duration is reported.
    done = build_eta(**{**kw, "task_status": "queued", "run_status": "completed"}, now=NOW.timestamp())
    assert done == {"typical_sec": 5000, "text": "usually takes 1h23m20s"}
    assert build_eta(**{**kw, "run_status": "completed", "predicted_sec": None}) is None


def _db(tmp_path: Path, status: str = "running"):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"]}), encoding="utf-8")
    db = open_db(tmp_path / "q.db")
    upsert_task(db, parse_task_yaml(path, Project(name="p", root=tmp_path, queue_dir=tmp_path)))
    db.execute("UPDATE tasks SET status = ? WHERE task_id = 't'", (status,))
    db.commit()
    return db


def _add_run(db, *, status: str, pct: float | None, rate: float | None, started: datetime, wall: int | None = None):
    finished = None if status == "running" else _iso(started + timedelta(seconds=wall or 0))
    mj = json.dumps({"loss": 0.1, "_progress": {"rate": rate, "at": 1.0}}) if rate else None
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, wall_sec, progress_pct, metrics_json, "
        "log_path) VALUES ('p', 't', ?, ?, ?, ?, ?, ?, '')",
        (_iso(started), finished, status, wall, pct, mj),
    )
    run_id = db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    db.execute("UPDATE tasks SET last_run_id = ? WHERE task_id = 't'", (run_id,))
    db.commit()


def test_task_eta_reads_the_latest_run(tmp_path: Path) -> None:
    db = _db(tmp_path, "interrupted")
    assert task_eta(db, "p", "t") is None  # never ran, no history
    assert task_eta(db, "p", "nope") is None
    _add_run(db, status="interrupted", pct=60, rate=None, started=NOW - timedelta(hours=1), wall=1200)
    eta = task_eta(db, "p", "t")
    assert eta is not None and eta["at_stop"] and eta["basis"] == "average" and eta["remaining_sec"] == 800
    db.close()


def test_wait_reports_eta_and_puts_it_in_the_timeout_hint(tmp_path: Path) -> None:
    db = _db(tmp_path, "running")
    _add_run(db, status="running", pct=10, rate=0.02, started=datetime.now(UTC) - timedelta(minutes=5))
    db.close()
    res = wait_for(lambda: open_db(tmp_path / "q.db"), ["p/t"], timeout_sec=0.01, poll_sec=0.01)
    assert res["timed_out"]
    eta = res["tasks"][0]["eta"]
    assert eta["remaining_sec"] == 4500 and eta["basis"] == "rate"
    assert res["hint"] == "still running; call wait again (~1h15m00s left (rate))"


def test_metrics_of_a_stopped_run_use_its_wall_time(tmp_path: Path) -> None:
    db = _db(tmp_path, "stopped")
    _add_run(db, status="stopped", pct=50, rate=None, started=NOW - timedelta(days=3), wall=600)
    m = build_run_metrics(db, "p", "t", home=tmp_path)
    assert m["elapsed_sec"] == 600 and m["eta_sec"] == 600  # not 3 days of "elapsed"
    assert m["eta"]["at_stop"] is True
    db.close()


def test_api_stop_returns_the_eta_at_the_moment_of_the_stop(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    path = tmp_path / "t.yaml"
    path.write_text(yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"]}), encoding="utf-8")
    upsert_task(db, parse_task_yaml(path, Project(name="p", root=tmp_path, queue_dir=tmp_path)))
    db.execute("UPDATE tasks SET status = 'running' WHERE task_id = 't'")
    db.commit()
    _add_run(db, status="running", pct=20, rate=0.1, started=datetime.now(UTC) - timedelta(minutes=1))
    code, body = http_request(H, "POST", "/api/stop/p/t")
    assert code == 200 and body["ok"]
    assert body["eta"]["remaining_sec"] == 800 and body["eta"]["basis"] == "rate"
    db.close()


def test_ui_eta_text_and_task_detail_api(tmp_path: Path) -> None:
    from tests.test_js_modules import _run

    fmt_js = (Path(__file__).resolve().parent.parent / "tqdaemon" / "static" / "js" / "util" / "format.js").as_uri()
    out = _run(
        "globalThis.i18n = { t: (k, v) => k + (v ? JSON.stringify(v) : '') };\n"
        f"import {{ etaText }} from {json.dumps(fmt_js)};\n"
        "console.log(JSON.stringify({"
        " stop: etaText({ remaining_sec: 7500, at_stop: true, progress_pct: 25.4 }),"
        " stopNoPct: etaText({ remaining_sec: 60, at_stop: true }),"
        " live: etaText({ remaining_sec: 90, finish_at: '2026-11-05T12:00:00Z' }),"
        " typical: etaText({ typical_sec: 3600 }), none: etaText(null) }));"
    )
    assert out["stop"] == 'eta.atStop{"left":"2h05m","pct":25}'
    assert out["stopNoPct"] == 'eta.atStopNoPct{"left":"1m00s"}'
    assert out["live"].startswith('eta.left{"left":"1m30s","at":')
    assert out["typical"] == 'eta.typical{"dur":"1h00m"}' and out["none"] == ""

    H, db, _ = make_handler_env(tmp_path)
    path = tmp_path / "t.yaml"
    path.write_text(yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"]}), encoding="utf-8")
    upsert_task(db, parse_task_yaml(path, Project(name="p", root=tmp_path, queue_dir=tmp_path)))
    db.execute("UPDATE tasks SET status = 'stopped' WHERE task_id = 't'")
    db.commit()
    _add_run(db, status="stopped", pct=40, rate=0.02, started=NOW - timedelta(hours=1), wall=1800)
    code, body = http_request(H, "GET", "/api/tasks/p/t")
    assert code == 200 and body["task"]["eta"]["at_stop"] is True and body["task"]["eta"]["remaining_sec"] == 3000
    db.close()
