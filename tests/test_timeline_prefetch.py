"""Timeline forecast prefetch helpers: bulk peaks and prior-run seconds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from taskqueue._db_tasks import bulk_prior_run_seconds
from taskqueue.db import open_db
from taskqueue.estimate import prefetch_exact_peaks, predict_task_resources
from tests.test_operator_features import _insert_run, _mk_task

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def test_prefetch_exact_peaks_matches_per_task(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    base = NOW - timedelta(days=1)
    for tid, ram in (("a", 2.0), ("b", 4.0), ("c", None)):
        _mk_task(db, "p", tid)
        if ram is not None:
            _insert_run(db, "p", tid, base.isoformat(), (base + timedelta(seconds=30)).isoformat())
            db.execute(
                "UPDATE runs SET peak_ram_gib = ?, status = 'completed' "
                "WHERE project = ? AND task_id = ?",
                (ram, "p", tid),
            )
    db.commit()
    peaks, checked = prefetch_exact_peaks(db, [("p", "a"), ("p", "b"), ("p", "c"), ("p", "missing")])
    assert checked == {("p", "a"), ("p", "b"), ("p", "c"), ("p", "missing")}
    assert peaks[("p", "a")]["ram_gib"] == 2.0
    assert peaks[("p", "b")]["ram_gib"] == 4.0
    assert ("p", "c") not in peaks
    assert ("p", "missing") not in peaks
    # Checked miss skips the exact-task SQL and falls through cleanly.
    pred = predict_task_resources(
        db, "p", "c", exact_peaks=peaks, exact_checked=checked
    )
    assert pred["source"] == "default"


def test_bulk_prior_run_seconds(tmp_path: Path):
    db = open_db(tmp_path / "q.db")
    _mk_task(db, "p", "job", status="running")
    t0 = NOW - timedelta(hours=2)
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, wall_sec) "
        "VALUES ('p','job',?,?, 'interrupted', 100.0)",
        (t0.isoformat(), (t0 + timedelta(seconds=100)).isoformat()),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status) VALUES ('p','job',?, 'running')",
        (NOW.isoformat(),),
    )
    db.commit()
    open_row = db.execute(
        "SELECT run_id, project, task_id FROM runs WHERE status = 'running'"
    ).fetchone()
    key = (open_row["project"], open_row["task_id"])
    got = bulk_prior_run_seconds(db, {key: open_row})
    assert got[key] == 100.0
