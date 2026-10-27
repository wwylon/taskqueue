"""Stalled vs slow: progress timing recorded by the runner and shown by tq status."""
from __future__ import annotations

import json

from taskqueue.log_issues import merge_metrics_payload, note_progress, progress_health


def test_note_progress_tracks_interval_and_rate():
    t = note_progress(None, None, 10.0, 1000.0)
    assert t == {"at": 1000.0}
    t = note_progress(t, 10.0, 20.0, 1060.0)       # +10 % in 60 s
    assert t["interval"] == 60.0 and abs(t["rate"] - 10 / 60) < 1e-9


def test_slow_is_not_stalled_but_long_silence_is():
    t = {"at": 1000.0, "interval": 60.0, "rate": 0.1}
    slow = progress_health(t, 50.0, 1100.0)        # 100 s since last change: within 3x interval
    assert slow == {"age_sec": 100, "stalled": False, "eta_sec": 500}
    hung = progress_health(t, 50.0, 1000.0 + 600)  # 10 min of silence vs 1 min usual
    assert hung["stalled"] is True and hung["age_sec"] == 600
    assert progress_health(None, 50.0, 1.0) == {}


def test_reserved_progress_key_cannot_be_set_by_task():
    p = merge_metrics_payload({"_progress": "forged", "loss": 1}, None, None, {"at": 5.0})
    assert p["_progress"] == {"at": 5.0} and p["loss"] == 1


def test_tq_status_shows_last_progress_and_stall_flag(tmp_path, capsys):
    import time

    from taskqueue._cli_admin import cmd_status
    from taskqueue.db import open_db

    db = open_db(tmp_path / "queue.db")
    now = time.time()
    db.execute("INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled) "
               "VALUES ('p','t','x','running','2026-01-01','2026-01-01',1)")
    db.execute("INSERT INTO runs (project, task_id, started_at, status, progress_pct, metrics_json) "
               "VALUES ('p','t',?, 'running', 40, ?)",
               (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3600)),
                json.dumps({"_progress": {"at": now - 900, "interval": 60.0, "rate": 0.05}})))
    db.commit()
    db.close()
    cmd_status(tmp_path, None)
    out = capsys.readouterr().out
    assert "last progress 15m" in out and "POSSIBLY STALLED" in out
    cmd_status(tmp_path, None, as_json=True)
    raw = capsys.readouterr().out
    data = json.loads(raw[raw.index("{"):])
    assert data["running"][0]["progress_health"]["stalled"] is True


def test_worker_chunk_records_metrics_series_and_progress_timing():
    from taskqueue.log_issues import apply_worker_chunk

    lines = ["(10.0%) start", 'TQ_METRIC {"loss": 0.5, "_series": "x"}', "plain line"]
    raw = apply_worker_chunk(None, lines, None, 1000.0)
    d = json.loads(raw)
    assert d["loss"] == 0.5 and d["_series"]["loss"] == [[1000.0, 0.5]]
    assert d["_progress"]["at"] == 1000.0
    raw2 = apply_worker_chunk(raw, ["(20.0%) more", "ERROR boom"], 10.0, 1060.0)
    d2 = json.loads(raw2)
    assert d2["_progress"]["interval"] == 60.0 and d2["log"]["errors"] == 1
    assert apply_worker_chunk(raw2, ["nothing new"], 20.0, 1100.0) is None


def test_status_payload_carries_progress_health_for_remote_runs(tmp_path):
    import time

    from taskqueue._db_runs import insert_run
    from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task

    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="t", dispatch="remote")
    rid = insert_run(db, "p", "t", str(tmp_path / "x.log"))
    db.execute("UPDATE tasks SET status='running'")
    db.execute("UPDATE runs SET progress_pct=30, metrics_json=? WHERE run_id=?",
               (json.dumps({"_progress": {"at": time.time() - 1200, "interval": 60.0}}), rid))
    db.commit()
    status, body = http_request(H, "GET", "/api/status")
    run = next(r for r in body["running"] if r["task_id"] == "t")
    assert run["progress_health"]["stalled"] is True and run["progress_health"]["age_sec"] >= 1200
