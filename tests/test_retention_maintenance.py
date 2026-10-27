"""Retention pass: FK-safe run pruning, batching, resource_history, audit, rotation."""

import os
import time
from datetime import datetime, timedelta, timezone

from taskqueue._cli_prune import (
    _per_project_retention,
    cap_finished_logs,
    prune_audit,
    prune_logs,
    prune_runs,
    rotate_logs,
    run_maintenance,
)
from taskqueue.db import open_db

UTC = timezone.utc


def _iso(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _run(db, project, task, days_ago, status="completed", log_path=None):
    cur = db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, log_path) VALUES (?,?,?,?,?,?)",
        (project, task, _iso(days_ago + 0.01), _iso(days_ago), status, log_path),
    )
    db.commit()
    return cur.lastrowid


def test_prune_runs_keeps_last_run_and_cleans_history(tmp_path):
    db = open_db(tmp_path / "q.db")
    old1 = _run(db, "p", "t", 400)
    old2 = _run(db, "p", "t", 300)
    fresh = _run(db, "p", "t", 1)
    db.execute("INSERT INTO resource_history (run_id, ts) VALUES (?, 1)", (old1,))
    db.execute("INSERT INTO resource_history (run_id, ts) VALUES (?, 1)", (fresh,))
    db.execute(
        "INSERT INTO tasks (project, task_id, command, enqueued_at, updated_at, last_run_id) VALUES ('p','t','x','2020-01-01','2020-01-01',?)", (old2,)
    )
    db.commit()
    cfg = {"retention_run_days": 180}
    dry = prune_runs(tmp_path, db, cfg, dry_run=True)
    assert dry["runs_deleted"] == 1  # old2 is tasks.last_run_id: protected
    res = prune_runs(tmp_path, db, cfg, dry_run=False)
    assert res["runs_deleted"] == 1
    left = sorted(r[0] for r in db.execute("SELECT run_id FROM runs"))
    assert left == [old2, fresh]
    assert db.execute("SELECT COUNT(*) FROM resource_history WHERE run_id=?", (old1,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM resource_history WHERE run_id=?", (fresh,)).fetchone()[0] == 1


def test_prune_runs_batches(tmp_path):
    db = open_db(tmp_path / "q.db")
    for _ in range(1200):
        db.execute(
            "INSERT INTO runs (project, task_id, started_at, finished_at, status) VALUES ('p','t',?,?,'failed')",
            (_iso(500), _iso(500)),
        )
    db.commit()
    res = prune_runs(tmp_path, db, {"retention_run_days": 30}, dry_run=False)
    assert res["runs_deleted"] == 1200
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_retention_projects_accepts_json_string():
    cfg = {"retention_log_days": 90, "retention_run_days": 180,
           "retention_projects": '{"p": {"run_days": 7}}'}
    assert _per_project_retention(cfg, "p") == (90, 7)
    assert _per_project_retention({"retention_projects": "not json"}, "p") == (90, 180)


def test_prune_logs_protects_listed_paths(tmp_path):
    d = tmp_path / "logs" / "p"
    d.mkdir(parents=True)
    old = d / "a.log"
    keep = d / "b.log"
    for f in (old, keep):
        f.write_text("x")
        t = time.time() - 400 * 86400
        os.utime(f, (t, t))
    res = prune_logs(tmp_path, {"retention_log_days": 30}, dry_run=False,
                     protected={str(keep.resolve())})
    assert res["logs_deleted"] == 1
    assert not old.exists() and keep.exists()


def test_audit_prune_and_rotation(tmp_path):
    db = open_db(tmp_path / "q.db")
    cols = [r[1] for r in db.execute("PRAGMA table_info(operator_audit)")]
    assert "ts" in cols
    db.execute("INSERT INTO operator_audit (ts, actor, action) VALUES (?, 'a', 'x')", (_iso(900),))
    db.execute("INSERT INTO operator_audit (ts, actor, action) VALUES (?, 'a', 'x')", (_iso(1),))
    db.commit()
    assert prune_audit(db, {"retention_audit_days": 365}, dry_run=True)["audit_deleted"] == 1
    assert prune_audit(db, {"retention_audit_days": 365}, dry_run=False)["audit_deleted"] == 1
    assert db.execute("SELECT COUNT(*) FROM operator_audit").fetchone()[0] == 1

    (tmp_path / "notify.jsonl").write_bytes(b"x" * 2048)
    assert rotate_logs(tmp_path, max_bytes=1024, dry_run=True) == ["notify.jsonl"]
    assert (tmp_path / "notify.jsonl").exists()
    rotate_logs(tmp_path, max_bytes=1024, dry_run=False)
    assert (tmp_path / "notify.jsonl.1").exists() and not (tmp_path / "notify.jsonl").exists()


def test_run_maintenance_end_to_end(tmp_path):
    db = open_db(tmp_path / "q.db")
    _run(db, "p", "t", 400)
    res = run_maintenance(tmp_path, db, {"retention_run_days": 30})
    assert res["runs_deleted"] == 1 and res["audit_deleted"] == 0


def test_cmd_prune_keeps_each_tasks_latest_log(tmp_path):
    from taskqueue._cli_prune import cmd_prune
    from taskqueue.config import db_path

    db = open_db(db_path(tmp_path))
    logs = tmp_path / "logs" / "p"
    logs.mkdir(parents=True)
    latest, stale = logs / "latest.log", logs / "stale.log"
    for f in (latest, stale):
        f.write_text("x")
        os.utime(f, (time.time() - 400 * 86400,) * 2)
    run_latest = _run(db, "p", "t", 400, log_path=str(latest))
    _run(db, "p", "t", 401, log_path=str(stale))
    db.execute(
        "INSERT INTO tasks (project, task_id, command, enqueued_at, updated_at, last_run_id) VALUES ('p','t','x','2020-01-01','2020-01-01',?)",
        (run_latest,),
    )
    db.execute("INSERT INTO config (key, value) VALUES ('retention_enabled', 'true') ON CONFLICT(key) DO UPDATE SET value='true'")
    db.execute(
        "INSERT INTO config (key, value) VALUES ('retention_log_max_bytes', '0') "
        "ON CONFLICT(key) DO UPDATE SET value='0'"
    )
    db.commit()
    db.close()
    assert cmd_prune(tmp_path, dry_run=False) == 0
    assert latest.exists() and not stale.exists()


def test_cap_finished_logs_shrinks_and_preserves_mtime(tmp_path):
    d = tmp_path / "logs" / "p"
    d.mkdir(parents=True)
    big = d / "big.log"
    # Head must be droppable; tail kept after newline align.
    big.write_bytes(b"HEAD\n" + b"y" * 2000 + b"\nTAIL_LINE\n")
    old_mtime = time.time() - 10 * 86400
    os.utime(big, (old_mtime, old_mtime))
    res = cap_finished_logs(
        tmp_path, {"retention_log_max_bytes": 64}, dry_run=False, unfinished=set()
    )
    assert res["logs_capped"] == 1
    assert res["logs_bytes_reclaimed"] > 0
    data = big.read_bytes()
    assert len(data) <= 64
    assert data.endswith(b"TAIL_LINE\n")
    assert abs(big.stat().st_mtime - old_mtime) < 1.5


def test_cap_finished_logs_skips_unfinished(tmp_path):
    d = tmp_path / "logs" / "p"
    d.mkdir(parents=True)
    active = d / "active.log"
    active.write_bytes(b"z" * 10_000)
    res = cap_finished_logs(
        tmp_path,
        {"retention_log_max_bytes": 100},
        dry_run=False,
        unfinished={str(active.resolve())},
    )
    assert res["logs_capped"] == 0
    assert active.stat().st_size == 10_000


def test_cap_off_when_max_bytes_zero(tmp_path):
    d = tmp_path / "logs" / "p"
    d.mkdir(parents=True)
    f = d / "big.log"
    f.write_bytes(b"z" * 10_000)
    res = cap_finished_logs(tmp_path, {"retention_log_max_bytes": 0}, dry_run=False)
    assert res["logs_capped"] == 0
    assert f.stat().st_size == 10_000


def test_worker_remove_reports_unknown_worker(tmp_path, capsys):
    from taskqueue._cli_worker import cmd_worker_remove
    from taskqueue.config import db_path

    open_db(db_path(tmp_path)).close()
    assert cmd_worker_remove(tmp_path, "ghost") == 1
    assert "not found" in capsys.readouterr().out
