"""Shared build_run_metrics contract."""

from __future__ import annotations

import time
from pathlib import Path

from taskqueue.db import open_db, record_resource_sample
from taskqueue.metrics_snapshot import build_run_metrics
from tests.helpers.master_worker import seed_queued_task


def test_build_run_metrics_history_and_gpu(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    seed_queued_task(db)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    db.execute(
        "UPDATE tasks SET status='running' WHERE task_id='T1'"
    )
    cur = db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, progress_pct, "
        "peak_cpu_pct, peak_ram_gib, peak_vram_gib) "
        "VALUES ('proj', 'T1', 'running', ?, 25.0, 80.0, 4.5, 1.2)",
        (now,),
    )
    run_id = cur.lastrowid
    db.commit()
    for i, util in enumerate((10.0, 40.0, 70.0)):
        record_resource_sample(
            db, run_id, cpu_pct=50.0 + i, ram_gib=2.0, vram_gib=1.0, gpu_util_pct=util
        )
    db.commit()

    m = build_run_metrics(
        db, "proj", "T1", home=tmp_path, samples=10, include_history=True
    )
    assert m["run_id"] == run_id
    assert m["status"] == "running"
    assert m["source"] == "db"
    assert m["resources"]["peak"]["ram_gib"] == 4.5
    assert m["resources"]["current"]["gpu_util_pct"] == 70.0
    assert m["resources"]["avg"]["gpu_util_pct"] == 40.0
    assert m["history_summary"]["n"] == 3
    assert len(m["history"]) == 3
    assert m["history"][-1]["gpu_util_pct"] == 70.0
    assert m["requires_gpu"] is False
    assert m["log"] == {"warnings": 0, "errors": 0, "other": 0}
    db.close()
