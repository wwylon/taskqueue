"""Per-project daily GPU-hour budget (max_gpu_hours_day)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from taskqueue.db import gpu_hours_today, set_project_limit, with_gpu_usage, get_project_limits
from taskqueue.scheduler import FilterContext, filter_project_limits
from tests.helpers.master_worker import make_handler_env


def _task(db, tid, gpu):
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, status, enabled, resources, "
        "enqueued_at, updated_at) VALUES ('p',?,'x','[]','queued',1,?,'n','n')",
        (tid, f'{{"gpu_vram_gib": {gpu}}}'),
    )


def _run(db, tid, start, end):
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at) "
        "VALUES ('p',?,'completed',?,?)",
        (tid, start.isoformat(), end.isoformat() if end else None),
    )


def test_gpu_hours_counts_only_gpu_runs_since_midnight(tmp_path):
    _, db, _ = make_handler_env(tmp_path)
    _task(db, "g", 8)
    _task(db, "c", 0)
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _run(db, "g", midnight + timedelta(minutes=1), midnight + timedelta(hours=2, minutes=1))  # 2h
    _run(db, "c", midnight + timedelta(minutes=1), midnight + timedelta(hours=5))            # cpu: ignored
    _run(db, "g", midnight - timedelta(hours=3), midnight + timedelta(hours=1))              # clipped to 1h
    _run(db, "g", midnight - timedelta(days=2), midnight - timedelta(days=2) + timedelta(hours=4))  # old
    db.commit()
    assert abs(gpu_hours_today(db, "p") - 3.0) < 0.01


def test_budget_blocks_gpu_task_only_when_exhausted(tmp_path):
    _, db, _ = make_handler_env(tmp_path)
    assert with_gpu_usage(db, "p", get_project_limits(db, "p")).get("gpu_hours_today") is None
    set_project_limit(db, "p", "max_gpu_hours_day", 2)
    row = {"project": "p"}
    over = FilterContext(project_limits={"max_gpu_hours_day": 2.0, "gpu_hours_today": 2.5})
    under = FilterContext(project_limits={"max_gpu_hours_day": 2.0, "gpu_hours_today": 0.5})
    assert not filter_project_limits(row, over, cpu=1, ram=0, gpu=8).ok
    assert "max_gpu_hours_day" in filter_project_limits(row, over, cpu=1, ram=0, gpu=8).reason
    assert filter_project_limits(row, over, cpu=1, ram=0, gpu=0).ok     # CPU-only still runs
    assert filter_project_limits(row, under, cpu=1, ram=0, gpu=8).ok
    assert "gpu_hours_today" in with_gpu_usage(db, "p", get_project_limits(db, "p"))
