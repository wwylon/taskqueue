"""Round 9 verified fixes (F-086–F-091 + Wave B/C helpers)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from taskqueue.config import Project
from taskqueue._db_tasks import claim_and_insert_local_run, upsert_task
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.notify import iter_notify_events
from taskqueue.queries import DEAD_DEP_STATUSES
from taskqueue.scheduler import FilterContext, filter_deps


def _project(name: str, root: Path) -> Project:
    qd = root / "experiments" / "operator_queue"
    qd.mkdir(parents=True, exist_ok=True)
    return Project(name=name, root=root, queue_dir=qd)


def _yaml(tmp_path: Path, project_name: str, task_id: str, **kwargs) -> Path:
    defaults = {
        "command": sys.executable,
        "args": ["-c", "print('hello')"],
        "resources": {"cpu_cores": 1},
        "priority": 50,
        "enabled": True,
    }
    defaults.update(kwargs)
    content = yaml.dump({"task_id": task_id, **defaults})
    p = tmp_path / f"{project_name}_{task_id}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_f090_disabled_dep_is_dead() -> None:
    ctx = FilterContext(
        dep_status=lambda p, t: "disabled" if t == "up" else "queued",
    )
    row = {
        "project": "p",
        "task_id": "down",
        "depends_on": json.dumps(["up"]),
        "depends_on_any": "[]",
    }
    assert "disabled" in DEAD_DEP_STATUSES
    fr = filter_deps(row, ctx)
    assert fr.ok is False
    assert "dead dependency" in (fr.reason or "")


def test_f088_claim_and_insert_atomic(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _yaml(tmp_path, "p", "t1")
    upsert_task(db, parse_task_yaml(path, proj))
    log = tmp_path / "logs" / "t1.log"
    log.parent.mkdir(parents=True)
    run_id = claim_and_insert_local_run(db, "p", "t1", str(log))
    assert run_id is not None and run_id > 0
    task = db.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()
    assert task["status"] == "running"
    run = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["status"] == "running"
    assert claim_and_insert_local_run(db, "p", "t1", str(log)) is None
    db.close()


def test_c1_notify_iter_exclusive_since(tmp_path: Path) -> None:
    path = tmp_path / "notify.jsonl"
    path.write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","project":"p","task_id":"a"}\n'
        '{"ts":"2026-01-01T00:00:01+00:00","project":"p","task_id":"b"}\n',
        encoding="utf-8",
    )
    ev = iter_notify_events(path, since="2026-01-01T00:00:00+00:00", limit=10)
    assert len(ev) == 1
    assert ev[0]["task_id"] == "b"
