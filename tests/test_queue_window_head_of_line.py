import json

from taskqueue._db_tasks import get_queued_tasks
from taskqueue.db import open_db


def _ins(db, tid, prio, deps="[]", status="queued"):
    db.execute(
        "INSERT INTO tasks (project, task_id, status, enabled, priority, enqueued_at, updated_at, command, "
        "depends_on, depends_on_any, yaml_source) VALUES ('p', ?, ?, 1, ?, '2026-01-01', '2026-01-01', 'echo', ?, '[]', 'x.yaml')",
        (tid, status, prio, deps),
    )


def test_blocked_head_does_not_hide_runnable_tail(tmp_path) -> None:
    db = open_db(tmp_path / "q.db")
    _ins(db, "gate", 0, status="running")
    for i in range(5):
        _ins(db, f"blocked{i}", 90, json.dumps(["gate"]))
    _ins(db, "free", 1)
    db.commit()
    smap = {(r["project"], r["task_id"]): r["status"] for r in db.execute("SELECT * FROM tasks")}
    plain = [r["task_id"] for r in get_queued_tasks(db, limit=3)]
    assert "free" not in plain
    wide = [r["task_id"] for r in get_queued_tasks(db, limit=3, status_map=smap)]
    assert "free" in wide
    assert get_queued_tasks(db, limit=3, status_map=smap, skip_projects={"p"})[:3] == get_queued_tasks(db, limit=3)


def test_queued_window_matches_two_pass_selection(tmp_path) -> None:
    """F-234: the one-scan window equals the old head + runnable-backfill selection."""
    import json
    import random

    from taskqueue._db_tasks import get_queued_window
    from taskqueue.db import open_db
    from taskqueue.scheduler import build_task_status_map

    rnd = random.Random(7)
    db = open_db(tmp_path / "w.db")
    statuses = ["queued", "queued", "stopped", "completed", "failed", "running"]
    for i in range(400):
        proj = f"p{i % 4}"
        deps = json.dumps([f"t{rnd.randrange(400)}"]) if rnd.random() < 0.4 else "[]"
        db.execute(
            "INSERT INTO tasks (project, task_id, command, status, enabled, priority, enqueued_at, updated_at, "
            "depends_on) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                proj,
                f"t{i}",
                "x",
                rnd.choice(statuses),
                int(rnd.random() < 0.9),
                rnd.randrange(5),
                f"2026-01-01T00:{i % 60:02d}:{i % 59:02d}+00:00",
                "2026-01-01",
                deps,
            ),
        )
    db.commit()
    for limit, skip in ((16, set()), (16, {"p1"}), (50, {"p0", "p2"}), (1000, set())):
        statuses_t = ("queued", "stopped")
        initial = get_queued_tasks(db, limit=limit, statuses=statuses_t, skip_projects=skip)
        smap = build_task_status_map(db, initial)
        old = (
            get_queued_tasks(db, limit=limit, statuses=statuses_t, status_map=smap, skip_projects=skip)
            if len(initial) >= limit
            else initial
        )
        new, _ = get_queued_window(db, limit=limit, statuses=statuses_t, skip_projects=skip)
        assert [(r["project"], r["task_id"]) for r in new] == [(r["project"], r["task_id"]) for r in old]
        assert set(new[0].keys()) == set(old[0].keys())
