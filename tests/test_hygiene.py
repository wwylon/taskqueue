"""Queue hygiene: churn, multi-use vs flaky, archive proposals + max_completed_runs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from tqdaemon.archive import archive_tasks
from tqdaemon.config import Project
from tqdaemon.db import open_db
from tqdaemon.hygiene import build_hygiene_report


def _conn(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    return open_db(home / "queue.db"), home


def _task(conn, project, tid, *, status="completed", enq, upd, yaml_source="", completed=0, total=0):
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, yaml_source, "
        "completed_run_count, total_run_count) VALUES (?,?,?,?,?,?,?,?,?)",
        (project, tid, "x", status, enq, upd, yaml_source, completed, total),
    )


def _run(conn, project, tid, *, started, finished=None, status="completed"):
    conn.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status) VALUES (?,?,?,?,?)",
        (project, tid, started, finished or started, status),
    )


def test_hygiene_churn_and_multi_use(tmp_path: Path) -> None:
    conn, _home = _conn(tmp_path)
    now = datetime(2026, 10, 24, 12, 0, 0)
    old = (now - timedelta(days=40)).isoformat()
    mid = (now - timedelta(days=20)).isoformat()
    recent = (now - timedelta(days=2)).isoformat()

    # one-shot stale completed
    _task(conn, "p", "once", enq=old, upd=old, completed=1, total=1)
    _run(conn, "p", "once", started=old, status="completed")

    # multi-use: 5 completions spanning >7 days (updated_at old so it is an archive candidate, then skipped)
    _task(conn, "p", "recur", enq=old, upd=old, completed=5, total=5)
    for day in [40, 30, 20, 10, 2]:
        t = (now - timedelta(days=day)).isoformat()
        _run(conn, "p", "recur", started=t, finished=t, status="completed")

    # flaky: many runs, one complete
    _task(conn, "p", "flake", enq=mid, upd=recent, completed=1, total=4)
    for i, st in enumerate(["failed", "failed", "failed", "completed"]):
        t = (now - timedelta(days=5 - i)).isoformat()
        _run(conn, "p", "flake", started=t, finished=t, status=st)

    # new task row in window
    _task(conn, "p", "fresh", status="queued", enq=recent, upd=recent, completed=0, total=0)
    conn.commit()

    rep = build_hygiene_report(conn, days=7, older_than_days=30, now=now)
    assert rep["kind"] == "hygiene"
    assert rep["churn"]["tasks_added"] >= 1
    assert any(m["task_id"] == "recur" for m in rep["multi_use"])
    assert any(f["task_id"] == "flake" for f in rep["flaky"])
    assert "agent_prompt" in rep and "Never auto-archive" in rep["agent_prompt"]
    # once is archivable (old, completed_run_count=1); recur skipped as multi-use
    props = {p["project"]: p for p in rep["archive_proposals"]}
    assert "p" in props
    assert props["p"]["archivable_count"] >= 1
    assert props["p"]["skipped_multi_use_count"] >= 1
    assert "--max-completed-runs 1" in props["p"]["dry_run_command"]


def test_archive_max_completed_runs(tmp_path: Path) -> None:
    conn, home = _conn(tmp_path)
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    for tid, crc in (("keep_multi", 5), ("drop_once", 1)):
        f = qdir / f"{tid}.yaml"
        f.write_text(f"task_id: {tid}\ncommand: x\n", encoding="utf-8")
        conn.execute(
            "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, yaml_source, "
            "completed_run_count, total_run_count) VALUES ('p',?, 'x','completed','2026-01-01','2026-01-01',?,?,?)",
            (tid, str(f), crc, crc),
        )
    conn.commit()
    proj = Project(name="p", root=root, queue_dir=qdir)
    r = archive_tasks(
        conn,
        {"p": proj},
        home=home,
        older_than_days=30,
        max_completed_runs=1,
        dry_run=True,
        now="2026-10-24T00:00:00",
    )
    ids = {t["task_id"] for t in r["tasks"]}
    assert ids == {"drop_once"}
    assert r["max_completed_runs"] == 1


def test_run_counts_maintained_on_insert_finalize(tmp_path: Path) -> None:
    from tqdaemon.db import finalize_run_and_task, insert_run

    conn, _ = _conn(tmp_path)
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES ('p','t','x','queued','2026-01-01','2026-01-01')"
    )
    conn.commit()
    conn.execute("UPDATE tasks SET status='running' WHERE project='p' AND task_id='t'")
    conn.commit()
    rid = insert_run(conn, "p", "t", log_path="x.log")
    total = conn.execute("SELECT total_run_count FROM tasks WHERE task_id='t'").fetchone()[0]
    assert int(total) == 1
    ok = finalize_run_and_task(conn, rid, "p", "t", 0, "completed", clear_task_error=True, only_if_running=True)
    assert ok
    row = conn.execute("SELECT completed_run_count, total_run_count, status FROM tasks WHERE task_id='t'").fetchone()
    assert int(row["completed_run_count"]) == 1
    assert int(row["total_run_count"]) == 1
    assert row["status"] == "completed"


def test_tq_list_uses_total_run_count_column(tmp_path: Path) -> None:
    from tqdaemon._cli_task import _list_fetch_rows

    conn, _home = _conn(tmp_path)
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, total_run_count) "
        "VALUES ('p','t','x','completed','2026-01-01','2026-01-01',7)"
    )
    conn.commit()
    rows, _ = _list_fetch_rows(conn, None)
    assert rows and int(rows[0]["attempts"]) == 7
