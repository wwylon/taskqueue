"""Slice G — progress sanity, reports, diagnose."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue import errors as tqerr
from taskqueue.config import Project
from taskqueue.db import open_db, parse_task_yaml
from taskqueue._db_tasks import set_task_error, upsert_task
from taskqueue.diagnose import build_diagnose
from taskqueue.progress_sanity import (
    is_frozen,
    needs_frozen_attention,
    progress_is_suspect,
    sanity_snapshot,
)
from taskqueue.reports import build_report


def _project(name: str, root: Path) -> Project:
    return Project(
        name=name,
        root=root,
        queue_dir=root / "experiments" / "operator_queue",
    )


def test_frozen_requires_stdout_and_active_idle() -> None:
    assert is_frozen(100, 100, 60) is True
    assert is_frozen(100, 10, 60) is False
    assert is_frozen(10, 100, 60) is False


def test_frozen_attention_at_2x() -> None:
    assert needs_frozen_attention(119, 60) is False
    assert needs_frozen_attention(120, 60) is True


def test_progress_suspect_detects_lie() -> None:
    assert progress_is_suspect(10.0, 90.0, 1.0, 1.01) is True
    assert progress_is_suspect(10.0, 90.0, 1.0, 2.5) is False
    assert progress_is_suspect(10.0, 12.0, 1.0, 1.0) is False


def test_progress_suspect_skips_wsl_dispatched_tasks() -> None:
    # F-244: Windows host psutil cannot see WSL guest CPU; pct can advance
    # with near-zero host active_delta on legitimate arena runs.
    assert progress_is_suspect(0.0, 5.0, 0.0, 0.0, wsl_dispatched=True) is False
    assert progress_is_suspect(0.0, 5.0, 0.0, 0.0, wsl_dispatched=False) is True


def test_progress_suspect_skips_gpu_bound_tasks() -> None:
    # Regression for bug #2: a GPU-bound loop blocked on
    # cudaStreamSynchronize shows ~0 host CPU while genuinely advancing.
    # Same inputs that trip the CPU-only heuristic above must NOT flag a
    # GPU-requesting task.
    assert progress_is_suspect(10.0, 90.0, 1.0, 1.01, gpu_requested=True) is False
    # Still catches the lie for a task that didn't request a GPU.
    assert progress_is_suspect(10.0, 90.0, 1.0, 1.01, gpu_requested=False) is True


def test_sanity_snapshot_flags() -> None:
    snap = sanity_snapshot(
        idle_stdout_sec=200,
        active_idle_sec=200,
        threshold_sec=60,
        prev_pct=10.0,
        cur_pct=90.0,
        prev_active=1.0,
        cur_active=1.0,
    )
    assert snap["frozen"] is True
    assert snap["frozen_attention"] is True
    assert snap["progress_suspect"] is True


def test_sanity_snapshot_gpu_requested_skips_progress_suspect() -> None:
    snap = sanity_snapshot(
        idle_stdout_sec=5,
        active_idle_sec=5,
        threshold_sec=60,
        prev_pct=10.0,
        cur_pct=90.0,
        prev_active=1.0,
        cur_active=1.0,
        gpu_requested=True,
    )
    # Not frozen (stdout/CPU idle well under threshold) and progress_suspect
    # is suppressed for a GPU-bound task despite the same lying-shaped inputs
    # that trip it in test_sanity_snapshot_flags.
    assert snap["frozen"] is False
    assert snap["progress_suspect"] is False


def test_report_and_diagnose(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("demo", tmp_path)
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.dump({
            "task_id": "t1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "resources": {"cpu_cores": 1},
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_error(db, "demo", "t1", tqerr.RUN_PROGRESS_SUSPECT, block_reason="lie")
    db.execute(
        "UPDATE tasks SET status = 'failed' WHERE project = 'demo' AND task_id = 't1'"
    )
    from datetime import UTC, datetime, timedelta
    started = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    finished = (datetime.now(UTC) - timedelta(days=1, minutes=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, exit_code, log_path, status) "
        "VALUES ('demo','t1',?,?,1,'', 'failed')",
        (started, finished),
    )
    db.commit()

    rep = build_report(db, project="demo", days=30)
    assert rep["days"] == 30
    assert rep["error_codes"].get(tqerr.RUN_PROGRESS_SUSPECT) == 1
    assert rep["runs_by_status"].get("failed") == 1

    log = tmp_path / "run.log"
    log.write_text("line1\nline2\n", encoding="utf-8")
    db.execute(
        "UPDATE runs SET log_path = ? WHERE project = 'demo' AND task_id = 't1'",
        (str(log),),
    )
    db.commit()

    bundle = build_diagnose(db, "demo", "t1", home=tmp_path)
    assert bundle["task"]["task_id"] == "t1"
    assert bundle["task"]["last_error_code"] == tqerr.RUN_PROGRESS_SUSPECT
    assert "progress" in (bundle["error_description"] or "").lower()
    assert "line2" in bundle["log_tail"]
    db.close()


def test_error_catalog_has_slice_g_codes() -> None:
    codes = tqerr.all_codes()
    assert tqerr.RUN_FROZEN in codes
    assert tqerr.RUN_PROGRESS_SUSPECT in codes
