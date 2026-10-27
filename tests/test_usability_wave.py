"""Usability wave: force stop hygiene, group filter, deps sort."""
from __future__ import annotations

from pathlib import Path

from taskqueue._db_runs import force_task
from taskqueue.config import Project
from taskqueue._db_tasks import get_task, request_stop, set_task_status, upsert_task
from taskqueue.db import open_db, parse_task_yaml, set_project_frozen
from taskqueue.health import compute_health
from taskqueue.models import STATUS_QUEUED, STATUS_STOPPED
from taskqueue.web import _UNGROUPED, _get_queue_response


def _project(name: str, tmp: Path) -> Project:
    root = tmp / name
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)
    return Project(name=name, root=root, queue_dir=q)


def _write_task(proj: Project, task_id: str, extra: str = "") -> Path:
    path = proj.queue_dir / f"{task_id}.yaml"
    path.write_text(f"id: {task_id}\ncommand: echo hi\n{extra}", encoding="utf-8")
    return path


def test_force_clears_stop_requested(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _write_task(proj, "t1")
    upsert_task(db, parse_task_yaml(path, proj))
    request_stop(db, "p", "t1")
    assert get_task(db, "p", "t1")["stop_requested"] == 1
    assert force_task(db, "p", "t1") is None
    row = get_task(db, "p", "t1")
    assert row["status"] == STATUS_QUEUED
    assert row["force_run"] == 1
    assert row["stop_requested"] == 0


def test_force_after_stopped_clears_flag(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _write_task(proj, "t2")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_status(db, "p", "t2", STATUS_STOPPED)
    request_stop(db, "p", "t2")
    assert force_task(db, "p", "t2") is None
    assert get_task(db, "p", "t2")["stop_requested"] == 0


def test_queue_group_filter(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    upsert_task(db, parse_task_yaml(_write_task(proj, "a", "group: train\n"), proj))
    upsert_task(db, parse_task_yaml(_write_task(proj, "b", "group: train\n"), proj))
    upsert_task(db, parse_task_yaml(_write_task(proj, "c"), proj))
    assert _get_queue_response(db, project="p", limit=50)["total"] == 3
    train = _get_queue_response(db, project="p", group="train", limit=50)
    assert train["total"] == 2
    assert {t["task_id"] for t in train["tasks"]} == {"a", "b"}
    ung = _get_queue_response(db, project="p", group=_UNGROUPED, limit=50)
    assert ung["total"] == 1
    assert ung["tasks"][0]["task_id"] == "c"


def test_queue_hide_blocked_keeps_gated_queued_hides_frozen_projects(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    p1 = _project("p1", tmp_path)
    p2 = _project("p2", tmp_path)
    upsert_task(db, parse_task_yaml(_write_task(p1, "ok"), p1))
    upsert_task(db, parse_task_yaml(_write_task(p1, "gated"), p1))
    upsert_task(db, parse_task_yaml(_write_task(p2, "other"), p2))
    db.execute(
        "UPDATE tasks SET last_error_code = 'tq.block.deps' WHERE project = ? AND task_id = ?",
        ("p1", "gated"),
    )
    db.commit()
    everything = _get_queue_response(db, limit=50)
    assert everything["total"] == 3
    # Active keeps queued tq.block.* waits; only frozen/paused projects are excluded.
    active = _get_queue_response(db, hide_blocked=True, limit=50)
    assert active["total"] == 3
    assert {(t["project"], t["task_id"]) for t in active["tasks"]} == {
        ("p1", "ok"), ("p1", "gated"), ("p2", "other"),
    }
    set_project_frozen(db, "p2", True)
    frozen_hidden = _get_queue_response(db, hide_blocked=True, limit=50)
    assert {(t["project"], t["task_id"]) for t in frozen_hidden["tasks"]} == {("p1", "ok"), ("p1", "gated")}
    assert frozen_hidden["total"] == 2


def test_health_and_new_failed_skip_frozen_projects(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    p1 = _project("p1", tmp_path)
    p2 = _project("p2", tmp_path)
    upsert_task(db, parse_task_yaml(_write_task(p1, "bad"), p1))
    upsert_task(db, parse_task_yaml(_write_task(p2, "bad"), p2))
    db.execute(
        "UPDATE tasks SET status = 'failed', updated_at = '2026-10-17T08:00:00+00:00'"
    )
    db.commit()
    assert compute_health(db)["counts"]["new_failed"] == 2
    set_project_frozen(db, "p2", True)
    health = compute_health(db)
    assert health["counts"]["new_failed"] == 1
    assert health["new_failed"] == ["p1/bad"]
    # The badge's "new" set: failures that ended after the acknowledge time.
    new_only = _get_queue_response(db, new_failed=True, ack_at="2026-10-17T07:00:00+00:00", limit=50)
    assert {(t["project"], t["task_id"]) for t in new_only["tasks"]} == {("p1", "bad"), ("p2", "bad")}
    active_new = _get_queue_response(
        db, new_failed=True, hide_blocked=True, ack_at="2026-10-17T07:00:00+00:00", limit=50
    )
    assert {(t["project"], t["task_id"]) for t in active_new["tasks"]} == {("p1", "bad")}
    acked = _get_queue_response(db, new_failed=True, ack_at="2026-10-17T09:00:00+00:00", limit=50)
    assert acked["total"] == 0


def test_acknowledged_failure_without_run_stays_acknowledged(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    p1 = _project("p1", tmp_path)
    upsert_task(db, parse_task_yaml(_write_task(p1, "bad"), p1))
    db.execute(
        "UPDATE tasks SET status = 'failed', finished_at = '2026-10-01T08:00:00+00:00', "
        "updated_at = '2026-10-17T08:00:00+00:00'"
    )
    db.commit()
    ack = "2026-10-10T00:00:00+00:00"
    assert compute_health(db, ack_at=ack)["counts"]["new_failed"] == 0
    assert _get_queue_response(db, new_failed=True, ack_at=ack, limit=50)["total"] == 0


def test_prior_run_seconds_sums_runs_since_last_completion(tmp_path: Path) -> None:
    from taskqueue._db_tasks import prior_run_seconds

    db = open_db(tmp_path / "queue.db")
    rows = [
        (1, "completed", "2026-10-01T00:00:00+00:00", "2026-10-01T01:00:00+00:00"),
        (2, "stopped", "2026-10-02T00:00:00+00:00", "2026-10-02T00:10:00+00:00"),
        (3, "interrupted", "2026-10-02T01:00:00+00:00", "2026-10-02T01:05:00+00:00"),
        (4, "running", "2026-10-02T02:00:00+00:00", None),
    ]
    db.executemany(
        "INSERT INTO runs (run_id, project, task_id, status, started_at, finished_at) "
        "VALUES (?, 'p', 't', ?, ?, ?)",
        rows,
    )
    db.commit()
    assert prior_run_seconds(db, "p", "t", 4) == 900.0
    assert prior_run_seconds(db, "p", "t", 2) == 0.0
    assert prior_run_seconds(db, "p", "t", None) == 0.0


def test_history_active_skips_frozen_projects(tmp_path: Path) -> None:
    from taskqueue.web import _get_history_response

    db = open_db(tmp_path / "queue.db")
    now = "2026-10-17T08:00:00+00:00"
    db.executemany(
        "INSERT INTO runs (project, task_id, status, started_at) VALUES (?,?,?,?)",
        [("p1", "a", "completed", now), ("p2", "b", "completed", now), ("p2", "c", "completed", now)],
    )
    db.commit()
    set_project_frozen(db, "p2", True)
    assert _get_history_response(db, limit=50)["total"] == 3
    active = _get_history_response(db, limit=50, hide_blocked=True)
    assert active["total"] == 1
    assert [r["project"] for r in active["runs"]] == ["p1"]
    # An explicit project choice wins over the active view.
    assert _get_history_response(db, project="p2", limit=50, hide_blocked=True)["total"] == 2


def test_queue_deps_sort_promotes_blockers(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    upsert_task(db, parse_task_yaml(_write_task(proj, "root"), proj))
    upsert_task(db, parse_task_yaml(_write_task(proj, "mid", "depends_on: [root]\n"), proj))
    upsert_task(db, parse_task_yaml(_write_task(proj, "leaf", "depends_on: [mid]\n"), proj))
    result = _get_queue_response(db, project="p", sort="deps", limit=50)
    ids = [t["task_id"] for t in result["tasks"]]
    assert ids.index("root") < ids.index("mid")
    assert ids.index("mid") < ids.index("leaf")


def test_set_task_error_fills_describe(tmp_path: Path) -> None:
    from taskqueue import errors as tqerr
    from taskqueue.db import set_task_error

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    path = _write_task(proj, "cj")
    upsert_task(db, parse_task_yaml(path, proj))
    set_task_error(db, "p", "cj", tqerr.INFRA_CLOCK_JUMP)
    row = get_task(db, "p", "cj")
    assert row["last_error_code"] == tqerr.INFRA_CLOCK_JUMP
    assert row["block_reason"]
    assert not str(row["block_reason"]).startswith("tq.")
    assert "slept" in row["block_reason"].lower() or "re-queued" in row["block_reason"].lower()


def test_open_db_migrate_false_skips_when_current(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    open_db(path)
    conn = open_db(path, migrate=False)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    conn.close()
