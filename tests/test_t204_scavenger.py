"""T-204: ``class: scavenger`` — idle-only admission + cooperative preemption."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from taskqueue import errors as tqerr
from taskqueue._db_tasks import upsert_task
from taskqueue.config import Project
from taskqueue.daemon import _preempt_scavenger
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.scheduler import _local_admit_queued


def _project(tmp_path: Path, name: str = "p") -> Project:
    root = tmp_path / name
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    return Project(name=name, root=root, queue_dir=qdir)


def _yaml(proj: Project, task_id: str, body: str = "") -> Path:
    p = proj.queue_dir / f"{task_id}.yaml"
    p.write_text(
        f"task_id: {task_id}\ncommand: python\nargs: ['-c', 'pass']\n{body}",
        encoding="utf-8",
    )
    return p


def _task_row(db, task_id: str):
    return db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()


def test_parse_class_default_and_scavenger(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert parse_task_yaml(_yaml(proj, "a"), proj).task_class == "normal"
    t = parse_task_yaml(_yaml(proj, "b", "class: scavenger\n"), proj)
    assert t.task_class == "scavenger"
    db = open_db(tmp_path / "q.db")
    upsert_task(db, t)
    assert _task_row(db, "b")["task_class"] == "scavenger"


def test_parse_rejects_bad_class_and_at_most_once(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    with pytest.raises(ValueError, match="class must be"):
        parse_task_yaml(_yaml(proj, "c", "class: bogus\n"), proj)
    with pytest.raises(ValueError, match="at_most_once"):
        parse_task_yaml(
            _yaml(proj, "d", "class: scavenger\nexecution_semantics: at_most_once\n"), proj
        )


def test_error_codes_registered() -> None:
    codes = tqerr.all_codes()
    assert tqerr.BLOCK_SCAVENGER in codes and tqerr.STOP_SCAVENGER in codes
    assert tqerr.block_kind(tqerr.BLOCK_SCAVENGER) == "resources"


_SYS = {
    "cpu_cores": 16, "ram_gib": 64.0, "gpu_vram_total_gib": 0.0, "gpus": [],
    "cpu_used_cores_ema": 0.0,
}


def _admit(db, tmp_path, projects, cfg=None):
    full = {"max_running_tasks": 1, "admit_max_per_tick": 1}
    full.update(cfg or {})
    _local_admit_queued(db, tmp_path, projects, {}, dict(_SYS), full, 10.0, False)


def _setup_busy(tmp_path: Path):
    """One running task fills ``max_running_tasks=1``; a normal and a scavenger wait."""
    proj = _project(tmp_path)
    db = open_db(tmp_path / "q.db")
    for tid, body in (("busy", ""), ("norm", "priority: 10\n"), ("scav", "class: scavenger\npriority: 99\n")):
        upsert_task(db, parse_task_yaml(_yaml(proj, tid, body), proj))
    db.execute("UPDATE tasks SET status='running' WHERE task_id='busy'")
    db.commit()
    return proj, db


def test_scavenger_waits_when_normal_work_is_capacity_blocked(tmp_path: Path) -> None:
    proj, db = _setup_busy(tmp_path)
    _admit(db, tmp_path, {"p": proj})
    norm, scav = _task_row(db, "norm"), _task_row(db, "scav")
    assert norm["last_error_code"] == tqerr.BLOCK_RESOURCES
    # Despite priority 99 the scavenger is judged after normal work and yields.
    assert scav["last_error_code"] == tqerr.BLOCK_SCAVENGER
    assert "p/norm" in scav["block_reason"]


def test_scavenger_not_blocked_by_scavenger_contention(tmp_path: Path) -> None:
    proj, db = _setup_busy(tmp_path)
    db.execute("UPDATE tasks SET enabled=0 WHERE task_id='norm'")
    db.commit()
    _admit(db, tmp_path, {"p": proj})
    # Only the global cap blocks it (normal reason), not scavenger contention.
    assert _task_row(db, "scav")["last_error_code"] == tqerr.BLOCK_RESOURCES


class _FakeRun:
    def __init__(self, task_row):
        self.task_row = task_row
        self.stop_reason = None
        self.suspended = 0

    def request_cooperative_suspend(self, home):
        self.suspended += 1


def _preempt_setup(tmp_path: Path):
    proj, db = _setup_busy(tmp_path)
    db.execute("UPDATE tasks SET status='running' WHERE task_id='scav'")
    db.execute(
        "UPDATE tasks SET last_error_code=?, block_reason='max_running_tasks=1 reached' "
        "WHERE task_id='norm'",
        (tqerr.BLOCK_RESOURCES,),
    )
    db.commit()
    run = _FakeRun(_task_row(db, "scav"))
    return db, {("p", "scav"): run}, run


def test_preempt_suspends_one_scavenger_with_cooldown(tmp_path: Path) -> None:
    db, active, run = _preempt_setup(tmp_path)
    state = {"at": 0.0}
    cfg = {"scavenger_preempt_cooldown_sec": 60}
    assert _preempt_scavenger(db, tmp_path, active, state, cfg, 1000.0) is True
    assert run.stop_reason == "scavenger_preempt" and run.suspended == 1
    assert _task_row(db, "scav")["stop_requested"] == 1
    db.execute("UPDATE tasks SET stop_requested=0 WHERE task_id='scav'")
    db.commit()
    assert _preempt_scavenger(db, tmp_path, active, state, cfg, 1010.0) is False  # cooldown
    assert _preempt_scavenger(db, tmp_path, active, state, cfg, 1100.0) is True


def test_preempt_ignores_own_project_cap_blocks(tmp_path: Path) -> None:
    db, active, run = _preempt_setup(tmp_path)
    db.execute("UPDATE tasks SET block_reason='project max_tasks=1 reached' WHERE task_id='norm'")
    db.commit()
    assert _preempt_scavenger(db, tmp_path, active, {"at": 0.0}, {}, 1000.0) is False
    assert run.suspended == 0


def test_preempt_noop_without_scavenger_or_contender(tmp_path: Path) -> None:
    db, active, run = _preempt_setup(tmp_path)
    assert _preempt_scavenger(db, tmp_path, {}, {"at": 0.0}, {}, 1000.0) is False
    db.execute("UPDATE tasks SET last_error_code=NULL WHERE task_id='norm'")
    db.commit()
    assert _preempt_scavenger(db, tmp_path, active, {"at": 0.0}, {}, 1000.0) is False
