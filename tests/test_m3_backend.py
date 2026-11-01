"""M3 backend speed regressions (F-159 through F-163)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.scheduler import build_task_status_map, worker_slot_reserved_many
from taskqueue.yaml_sync import clear_yaml_mtime_cache, yaml_cache_stats


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_admission_index_is_composite(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    cols = [row["name"] for row in conn.execute("PRAGMA index_info(idx_tasks_admission)").fetchall()]
    assert cols == ["status", "enabled", "priority", "enqueued_at"]


def test_status_map_is_candidate_and_dependency_scoped(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    now = _now()
    conn.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, depends_on) "
        "VALUES (?, ?, 'python', ?, ?, ?, ?)",
        [
            ("p", "candidate", "queued", now, now, '["dep"]'),
            ("p", "dep", "completed", now, now, "[]"),
            ("other", "unrelated", "failed", now, now, "[]"),
        ],
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE project='p' AND task_id='candidate'").fetchone()
    result = build_task_status_map(conn, [row])
    assert result == {("p", "candidate"): "queued", ("p", "dep"): "completed"}


def test_worker_slot_reserved_many_batches_workers(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    now = _now()
    conn.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, last_seen) "
        "VALUES ('w1', 'x', 'x', 8, 4, 2, ?)",
        (now,),
    )
    conn.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, resources) "
        "VALUES (?, ?, 'python', 'running', ?, ?, ?)",
        [
            ("p", "a", now, now, '{"cpu_cores":2,"ram_gib":3,"gpu_vram_gib":1}'),
            ("p", "b", now, now, '{"cpu_cores":1,"ram_gib":2,"gpu_vram_gib":0.5}'),
        ],
    )
    conn.executemany(
        "INSERT INTO runs (project, task_id, started_at, status, worker_id) VALUES (?, ?, ?, 'running', 'w1')",
        [("p", "a", now), ("p", "b", now)],
    )
    conn.commit()
    assert worker_slot_reserved_many(conn, ["w1", "w2"]) == {
        "w1": (3.0, 5.0, 1.5),
        "w2": (0.0, 0.0, 0.0),
    }


def test_yaml_cache_stats_reset() -> None:
    clear_yaml_mtime_cache()
    stats = yaml_cache_stats()
    assert stats["entries"] == 0
    assert stats["bytes"] == 0
    assert stats["max_bytes"] > 0


def test_progress_helpers_support_batched_commit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "queue.db")
    from taskqueue._db_runs import update_run_metrics, update_run_progress

    now = _now()
    run_id = conn.execute(
        "INSERT INTO runs (project, task_id, started_at, status) VALUES ('p','t',?,'running')",
        (now,),
    ).lastrowid
    update_run_progress(conn, run_id, 10, "line", commit=False)
    update_run_metrics(conn, run_id, {"x": 1}, commit=False)
    assert conn.in_transaction
    conn.commit()
    assert conn.execute("SELECT progress_pct FROM runs WHERE run_id=?", (run_id,)).fetchone()[0] == 10
