from datetime import UTC, datetime
from types import SimpleNamespace

from taskqueue.config import Project
from taskqueue.db import get_task, open_db
from taskqueue.scheduler import _local_admit_queued


def test_running_task_charged_by_estimate_against_global_ram(tmp_path) -> None:
    """A running task with no declared RAM still counts its estimate toward max_ram_gib."""
    conn = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    for tid, status in (("r1", "running"), ("t2", "queued")):
        conn.execute(
            "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled) "
            "VALUES ('p',?,'true',?,?,?,1)",
            (tid, status, now, now),
        )
    conn.commit()
    projects = {"p": Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")}
    system = {"cpu_total_cores": 8, "ram_total_gib": 64.0, "gpu_vram_total_gib": 0.0, "gpus": []}
    cfg = {
        "cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 80, "ram_hard_limit_pct": 95,
        "max_ram_gib": 1.5, "default_ram_gib": 1.0, "max_concurrent_unknown": 0,
    }
    run = SimpleNamespace(
        elapsed_sec=lambda: 1.0, current_ram_gib=0.0, current_vram_gib=0.0, stop_reason=None,
    )
    _local_admit_queued(conn, tmp_path, projects, {("p", "r1"): run}, system, cfg, 30, False)
    row = get_task(conn, "p", "t2")
    assert row["status"] == "queued"
    assert "max_ram_gib" in (row["block_reason"] or "")


def test_max_attempts_exhausted_tasks_marked_failed_in_one_pass(tmp_path) -> None:
    conn = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    for tid in ("a", "b"):
        conn.execute(
            "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled, max_attempts) "
            "VALUES ('p',?,'true','queued',?,?,1,1)",
            (tid, now, now),
        )
        conn.execute("INSERT INTO runs (project, task_id, started_at, status) VALUES ('p',?,?,'failed')", (tid, now))
    conn.commit()
    projects = {"p": Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")}
    system = {"cpu_total_cores": 8, "ram_total_gib": 64.0, "gpu_vram_total_gib": 0.0, "gpus": []}
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 80, "ram_hard_limit_pct": 95}
    _local_admit_queued(conn, tmp_path, projects, {}, system, cfg, 30, False)
    for tid in ("a", "b"):
        row = get_task(conn, "p", tid)
        assert row["status"] == "failed" and row["finished_at"]
        assert "max_attempts=1" in (row["block_reason"] or "")
