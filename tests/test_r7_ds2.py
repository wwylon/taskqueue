"""Round 7b (ds2.md): wake-on-enqueue, SSE snapshot, depends_on_any, spawned_by."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskqueue.db import open_db, parse_task_yaml
from taskqueue._db_tasks import retry_task, upsert_task
from taskqueue.finalize_hooks import apply_finalize_wake_hooks
from taskqueue.queries import find_broken_deps
from taskqueue.scheduler import FilterContext, filter_deps
from taskqueue.web.sse import SSEMixin, clear_status_snapshot_cache


def _write_yaml(proj: Path, task_id: str, body: str) -> Path:
    q = proj / "experiments" / "operator_queue"
    q.mkdir(parents=True, exist_ok=True)
    p = q / f"{task_id}.yaml"
    p.write_text(
        f"task_id: {task_id}\ncommand: python\nargs: ['-c', 'pass']\n{body}",
        encoding="utf-8",
    )
    return p


def _project(tmp_path: Path, name: str = "p"):
    from taskqueue.config import Project

    root = tmp_path / name
    root.mkdir()
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    return Project(name=name, root=root, queue_dir=qdir)


# --- T-200 wake ---


def test_retry_task_calls_wake_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from taskqueue import sched_wake

    woken: list[bool] = []

    def _wake() -> None:
        woken.append(True)

    monkeypatch.setattr(sched_wake, "wake_scheduler", _wake)
    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enabled, "
        "enqueued_at, updated_at) VALUES ('p','t','python','completed',1,?,?)",
        (now, now),
    )
    db.commit()
    assert retry_task(db, "p", "t") is True
    assert woken == [True]
    db.close()


# --- T-201 SSE snapshot cache ---


def test_shared_status_snapshot_reuses_within_ttl() -> None:
    clear_status_snapshot_cache()

    class H(SSEMixin):
        def __init__(self) -> None:
            self.n = 0

        def _get_status(self) -> dict:
            self.n += 1
            return {"n": self.n, "running": [], "queued_count": 0}

    h = H()
    a = h._shared_status()
    b = h._shared_status()
    assert a is b
    assert h.n == 1
    clear_status_snapshot_cache()
    _ = h._shared_status()
    assert h.n == 2


# --- T-202 depends_on_any ---


def test_parse_depends_on_any(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    path = _write_yaml(
        proj.root,
        "fan",
        "depends_on_any: [a, b]\ndepends_on: [gate]\n",
    )
    task = parse_task_yaml(path, proj)
    assert task.depends_on == ("gate",)
    assert task.depends_on_any == ("a", "b")
    db = open_db(tmp_path / "q.db")
    upsert_task(db, task)
    row = db.execute(
        "SELECT depends_on, depends_on_any FROM tasks WHERE task_id='fan'"
    ).fetchone()
    assert json.loads(row["depends_on"]) == ["gate"]
    assert json.loads(row["depends_on_any"]) == ["a", "b"]
    db.close()


def _ctx(statuses: dict[tuple[str, str], str]) -> FilterContext:
    return FilterContext(
        dep_status=lambda p, t: statuses.get((p, t)),
        skip_node_capacity=True,
    )


def test_filter_deps_any_admits_when_one_completed() -> None:
    row = {
        "project": "p",
        "depends_on": "[]",
        "depends_on_any": json.dumps(["a", "b", "c"]),
    }
    ctx = _ctx({("p", "a"): "queued", ("p", "b"): "completed", ("p", "c"): "failed"})
    assert filter_deps(row, ctx).ok is True


def test_filter_deps_any_waits_when_none_completed() -> None:
    row = {
        "project": "p",
        "depends_on": "[]",
        "depends_on_any": json.dumps(["a", "b"]),
    }
    ctx = _ctx({("p", "a"): "queued", ("p", "b"): "running"})
    r = filter_deps(row, ctx)
    assert r.ok is False
    assert "any of" in (r.reason or "")


def test_filter_deps_and_plus_any() -> None:
    row = {
        "project": "p",
        "depends_on": json.dumps(["gate"]),
        "depends_on_any": json.dumps(["a", "b"]),
    }
    ctx = _ctx({("p", "gate"): "queued", ("p", "a"): "completed"})
    assert filter_deps(row, ctx).ok is False
    ctx2 = _ctx({("p", "gate"): "completed", ("p", "a"): "completed"})
    assert filter_deps(row, ctx2).ok is True


def test_find_broken_deps_any_only_when_all_dead(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"

    def seed(tid: str, status: str, *, any_deps=None) -> None:
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, enabled, "
            "depends_on, depends_on_any, enqueued_at, updated_at) "
            "VALUES ('p',?,?, 'true',1,'[]',?,?,?)",
            (tid, status, json.dumps(any_deps or []), now, now),
        )

    seed("a", "failed")
    seed("b", "queued")
    seed("ok", "queued", any_deps=["a", "b"])  # b still alive → not broken
    seed("x", "failed")
    seed("y", "stopped")
    seed("bad", "queued", any_deps=["x", "y"])  # all dead → broken
    db.commit()
    broken = {b["task_id"] for b in find_broken_deps(db, project="p")}
    assert "ok" not in broken
    assert "bad" in broken
    db.close()


def test_deps_api_or_edges(tmp_path: Path) -> None:
    from taskqueue.web import _get_deps_response

    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enabled, "
        "depends_on, depends_on_any, enqueued_at, updated_at) "
        "VALUES ('p','leaf','queued','true',1,'[]',?,?,?)",
        (json.dumps(["a", "b"]), now, now),
    )
    db.commit()
    result = _get_deps_response(db, project="p")
    any_edges = [e for e in result["edges"] if e.get("kind") == "any"]
    assert len(any_edges) == 2
    db.close()


# --- T-203 spawned_by ---


def test_on_complete_sets_spawned_by(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    now = "2026-01-01T00:00:00+00:00"
    for tid, st, oc in (
        ("parent", "completed", json.dumps(["child"])),
        ("child", "completed", "[]"),
    ):
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, enabled, "
            "on_complete, enqueued_at, updated_at) VALUES ('p',?,?, 'true',1,?,?,?)",
            (tid, st, oc, now, now),
        )
    db.commit()
    apply_finalize_wake_hooks(db, "p", "parent", "completed", run_teardown=False)
    row = db.execute(
        "SELECT status, spawned_by FROM tasks WHERE task_id='child'"
    ).fetchone()
    assert row["status"] == "queued"
    assert row["spawned_by"] == "p/parent"
    db.close()
