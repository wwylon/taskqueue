"""P3 / R-006 / R-008: multi-slot leftover RAM and T-031 single-claim races."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.worker_api import claim_next_remote_task


def _insert_task(
    db,
    project="proj",
    task_id="T1",
    status="queued",
    dispatch="remote",
    resources=None,
):
    now = datetime.now(UTC).isoformat()
    res = json.dumps(resources or {"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0})
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, max_attempts, enabled, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, 'true', '[]', ?, 50, ?, '[]', 0, 0, 1, ?, ?)",
        (project, task_id, status, dispatch, res, now, now),
    )
    db.commit()


def _insert_worker(db, worker_id="w1", ram_gib=16.0, load=None, **kwargs):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen, load_json) "
        "VALUES (?, 'windows', 'x86_64', ?, 8, 0.0, '[]', ?, 'online', ?, ?)",
        (
            worker_id,
            ram_gib,
            json.dumps(["proj"]),
            now,
            json.dumps(load or {}),
        ),
    )
    db.commit()
    return db.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()


def _claim(db_path, worker_row):
    return claim_next_remote_task(db_path, worker_row, lambda: {"proj": None})


def test_four_fit_fifth_queued(tmp_path: Path):
    """§28 #8–9: four 4 GiB tasks fit a 16 GiB worker; the fifth stays queued."""
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    for i in range(5):
        _insert_task(db, task_id=f"T{i}", resources={"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0})
    worker = _insert_worker(db, ram_gib=16.0)
    claimed = []
    for _ in range(5):
        worker = db.execute("SELECT * FROM workers WHERE worker_id='w1'").fetchone()
        result = _claim(db_path, worker)
        if result.get("task"):
            claimed.append(result["task"]["task_id"])
        else:
            break
    assert len(claimed) == 4
    queued = db.execute("SELECT task_id FROM tasks WHERE status='queued'").fetchall()
    running = db.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='running'").fetchone()["n"]
    assert running == 4
    assert len(queued) == 1


def test_live_load_heartbeat_reduces_leftover(tmp_path: Path):
    """R-008: heartbeat ram_used_gib is applied as reserved capacity."""
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 8.0, "gpu_vram_gib": 0.0})
    worker = _insert_worker(db, ram_gib=16.0, load={"ram_used_gib": 10.0})
    result = _claim(db_path, worker)
    assert result.get("task") is None


def test_t031_eight_workers_one_task(tmp_path: Path):
    """§28 #1: eight concurrent claims of one task create exactly one attempt."""
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_task(db, task_id="ONLY")
    workers = [_insert_worker(db, worker_id=f"w{i}", ram_gib=16.0) for i in range(8)]
    results: list[dict] = []
    lock = threading.Lock()

    def _go(wrow):
        r = claim_next_remote_task(db_path, wrow, lambda: {"proj": None})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_go, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    hits = [r for r in results if r.get("task")]
    assert len(hits) == 1
    n_running = db.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='running'").fetchone()["n"]
    n_runs = db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    assert n_running == 1
    assert n_runs == 1
