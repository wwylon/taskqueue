"""tq archive: export old finished tasks to JSONL, move their YAML aside, drop the DB rows."""

from __future__ import annotations

import json
from pathlib import Path

from taskqueue.archive import archive_tasks
from taskqueue.config import Project
from taskqueue.db import open_db


def _setup(tmp_path: Path):
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    conn = open_db(tmp_path / "home" / "queue.db")
    (tmp_path / "home").mkdir(exist_ok=True)
    rows = [
        ("old_done", "completed", "2026-01-01T00:00:00"),
        ("old_fail", "failed", "2026-01-01T00:00:00"),
        ("new_done", "completed", "2026-10-20T00:00:00"),
        ("old_run", "running", "2026-01-01T00:00:00"),
    ]
    for tid, status, upd in rows:
        f = qdir / f"{tid}.yaml"
        f.write_text(f"task_id: {tid}\ncommand: x\n", encoding="utf-8")
        conn.execute(
            "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, yaml_source) "
            "VALUES ('p', ?, 'x', ?, ?, ?, ?)",
            (tid, status, upd, upd, str(f)),
        )
        conn.execute(
            "INSERT INTO runs (project, task_id, started_at, status) VALUES ('p', ?, ?, ?)", (tid, upd, status)
        )
    conn.commit()
    return conn, Project(name="p", root=root, queue_dir=qdir), qdir


def test_archives_only_old_completed_by_default(tmp_path: Path) -> None:
    conn, proj, qdir = _setup(tmp_path)
    home = tmp_path / "home"
    r = archive_tasks(conn, {"p": proj}, home=home, older_than_days=30, now="2026-10-24T00:00:00")
    assert r["archived"] == 1 and r["tasks"] == [{"project": "p", "task_id": "old_done"}]
    left = {x[0] for x in conn.execute("SELECT task_id FROM tasks")}
    assert left == {"old_fail", "new_done", "old_run"}
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE task_id='old_done'").fetchone()[0] == 0
    assert not (qdir / "old_done.yaml").exists() and (qdir / "archive" / "old_done.yaml").exists()
    lines = (home / "archive" / "p.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert rec["task"]["task_id"] == "old_done" and len(rec["runs"]) == 1


def test_dry_run_and_status_override(tmp_path: Path) -> None:
    conn, proj, qdir = _setup(tmp_path)
    home = tmp_path / "home"
    r = archive_tasks(
        conn, {"p": proj}, home=home, older_than_days=30, statuses=("completed", "failed"),
        dry_run=True, now="2026-10-24T00:00:00",
    )
    assert r["archived"] == 2 and r["dry_run"] is True
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 4
    assert (qdir / "old_done.yaml").exists() and not (home / "archive").exists()


def test_sync_does_not_resurrect_archived(tmp_path: Path) -> None:
    from taskqueue.yaml_sync import _sync_project, clear_yaml_mtime_cache

    clear_yaml_mtime_cache()
    conn, proj, qdir = _setup(tmp_path)
    archive_tasks(conn, {"p": proj}, home=tmp_path / "home", older_than_days=30, now="2026-10-24T00:00:00")
    _sync_project(conn, proj, force=True)
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE task_id='old_done'").fetchone()[0] == 0


def test_api_archive_route_and_validation(tmp_path: Path) -> None:
    from tests.helpers.master_worker import http_request, make_handler_env

    conn, proj, qdir = _setup(tmp_path)
    conn.close()
    H, db, _ = make_handler_env(tmp_path / "h", projects={"p": proj})
    for tid, st in (("a", "completed"), ("b", "failed")):
        db.execute(
            "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
            "VALUES ('p', ?, 'x', ?, '2020-01-01T00:00:00', '2020-01-01T00:00:00')",
            (tid, st),
        )
    db.commit()
    code, data = http_request(H, "POST", "/api/cli/archive", {"dry_run": True})
    assert code == 200 and data["archived"] == 1 and data["dry_run"] is True
    code, data = http_request(H, "POST", "/api/cli/archive", {"statuses": ["completed", "failed"]})
    assert code == 200 and data["archived"] == 2
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE project='p'").fetchone()[0] == 0
    assert http_request(H, "POST", "/api/cli/archive", {"projects": ["nope"]})[0] == 404
    assert http_request(H, "POST", "/api/cli/archive", {"older_than_days": "x"})[0] == 400
    db.close()
