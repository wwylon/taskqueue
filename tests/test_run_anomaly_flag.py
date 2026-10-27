"""Run anomaly flag: History rows carry `anomalies`, and a completed outlier fires run_anomaly."""
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.system_alerts import on_run_terminal
from taskqueue.web.responses import _get_history_response


def _add_run(db, project, task, ram, status="completed"):
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, peak_ram_gib, wall_sec) "
        "VALUES (?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', ?, ?, 60)",
        (project, task, status, ram),
    )
    db.commit()
    return cur.lastrowid


def _fired(db):
    return db.execute(
        "SELECT fire_count FROM system_alerts WHERE key='run_anomaly'"
    ).fetchone()["fire_count"]


def test_history_flags_outlier_and_alert_fires(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    for _ in range(6):
        _add_run(db, "p", "t", 4.0)
    normal = _add_run(db, "p", "t", 4.2)
    outlier = _add_run(db, "p", "t", 12.0)
    runs = {r["run_id"]: r for r in _get_history_response(db, limit=50)["runs"]}
    assert "anomalies" not in runs[normal]
    assert [a["metric"] for a in runs[outlier]["anomalies"]] == ["ram"]

    on_run_terminal(db, tmp_path, project="p", task_id="t", status="completed", run_id=normal)
    assert _fired(db) == 0
    on_run_terminal(db, tmp_path, project="p", task_id="t", status="completed", run_id=outlier)
    assert _fired(db) == 1


def test_no_flag_without_enough_history(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    for _ in range(3):
        _add_run(db, "p", "t", 4.0)
    big = _add_run(db, "p", "t", 40.0)
    on_run_terminal(db, tmp_path, project="p", task_id="t", status="completed", run_id=big)
    assert _fired(db) == 0
    assert "anomalies" not in _get_history_response(db, limit=1)["runs"][0]
