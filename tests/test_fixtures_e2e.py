"""Fixture e2e against temp TASKQUEUE_HOME + fixtures/tqdemo."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from taskqueue import errors as tqerr
from taskqueue.config import Project
from taskqueue._db_tasks import get_task, set_task_error, upsert_task
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.models import STATUS_COMPLETED, STATUS_FAILED, STATUS_QUEUED
from taskqueue.resources import ResourceBudget
from taskqueue.runner import ActiveRun
from tests.helpers.wait import wait_until

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tqdemo"


def _tqdemo() -> Project:
    return Project(
        name="tqdemo",
        root=FIXTURES,
        queue_dir=FIXTURES / "experiments" / "operator_queue",
    )


def _sync_one(db, task_id: str):
    proj = _tqdemo()
    path = proj.queue_dir / f"{task_id}.yaml"
    task = parse_task_yaml(path, proj)
    upsert_task(db, task)
    # Enable in DB without mutating fixture YAML on disk.
    db.execute(
        "UPDATE tasks SET enabled = 1, status = 'queued' WHERE project=? AND task_id=?",
        ("tqdemo", task_id),
    )
    db.commit()
    return get_task(db, "tqdemo", task_id)


def test_dummy_progress_completes(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    row = _sync_one(db, "dummy-progress")
    run = ActiveRun(
        conn=db,
        task_row=row,
        log_dir=tmp_path / "logs",
        cwd=FIXTURES,
        db_path=tmp_path / "queue.db",
    )
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll()
    run.finalize(STATUS_COMPLETED)
    assert get_task(db, "tqdemo", "dummy-progress")["status"] == STATUS_COMPLETED
    assert run.progress_pct is not None and run.progress_pct >= 100.0


def test_dummy_fail_nonzero(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    row = _sync_one(db, "dummy-fail")
    run = ActiveRun(
        conn=db,
        task_row=row,
        log_dir=tmp_path / "logs",
        cwd=FIXTURES,
        db_path=tmp_path / "queue.db",
    )
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll()
    assert run._exit_code != 0
    run.finalize(STATUS_FAILED, last_error_code=tqerr.RUN_NONZERO)
    row = get_task(db, "tqdemo", "dummy-fail")
    assert row["status"] == STATUS_FAILED
    assert row["last_error_code"] == tqerr.RUN_NONZERO


def test_dummy_ask_cpu_blocked_by_budget(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    row = _sync_one(db, "dummy-ask-cpu")
    assert row["status"] == STATUS_QUEUED
    system = {
        "cpu_total_cores": 4,
        "ram_total_gib": 16,
        "gpu_vram_total_gib": 0,
        "gpus": [],
    }
    cfg = {"cpu_limit_pct": 50, "gpu_limit_pct": 100, "ram_limit_pct": 80}
    budget = ResourceBudget(system, cfg)
    # Monkeypatch seed ram so fits only checks CPU
    budget.ram_reserved = 0.0
    res = yaml.safe_load((FIXTURES / "experiments/operator_queue/dummy-ask-cpu.yaml").read_text())[
        "resources"
    ]
    ok, reason = budget.fits(float(res["cpu_cores"]), 0.0, float(res.get("ram_gib", 0)))
    assert ok is False
    set_task_error(db, "tqdemo", "dummy-ask-cpu", tqerr.BLOCK_RESOURCES, block_reason=reason)
    row = get_task(db, "tqdemo", "dummy-ask-cpu")
    assert row["last_error_code"] == tqerr.BLOCK_RESOURCES
    assert row["status"] == STATUS_QUEUED


@pytest.mark.stress
def test_dummy_alloc_ram_stress(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    row = _sync_one(db, "dummy-alloc-ram")
    run = ActiveRun(
        conn=db,
        task_row=row,
        log_dir=tmp_path / "logs",
        cwd=FIXTURES,
        db_path=tmp_path / "queue.db",
    )
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    assert run.poll()
    run.finalize(STATUS_COMPLETED)
    assert get_task(db, "tqdemo", "dummy-alloc-ram")["status"] == STATUS_COMPLETED
