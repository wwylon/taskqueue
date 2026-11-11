"""tq doctor checks: levels, JSON output and exit code."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.helpers.master_worker import seed_queued_task, seed_worker
from tqdaemon import cli
from tqdaemon.doctor_checks import propose_remediations, run_checks


def test_fresh_home_warns_but_passes(tmp_path: Path) -> None:
    r = run_checks(tmp_path)
    by = {c["name"]: c["level"] for c in r["checks"]}
    assert r["ok"] is True and by["daemon"] == "warn" and by["database"] == "warn"


def test_corrupt_db_fails_and_json_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    from tqdaemon.config import db_path

    sqlite3.connect(db_path(tmp_path)).close()
    db_path(tmp_path).write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)  # header ok, pages garbage
    (tmp_path / "projects.yaml").write_text("projects:\n  p:\n    root: /nonexistent/xyz\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    rc = cli.main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    by = {c["name"]: c for c in out["checks"]}
    assert rc == 1 and by["database"]["level"] == "fail"
    assert by["projects"]["level"] == "warn" and "nonexistent" in by["projects"]["detail"]


def test_doctor_fix_dry_run_lists_retry_and_start(tmp_path: Path, monkeypatch, capsys) -> None:
    from tqdaemon.db import open_db

    conn = open_db(tmp_path / "queue.db")
    seed_queued_task(conn, project="p", task_id="t1", dispatch="local")
    conn.execute("UPDATE tasks SET status='failed', enabled=1 WHERE project='p' AND task_id='t1'")
    seed_worker(conn, "w-offline", status="offline")
    conn.close()
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    rc = cli.main(["doctor", "--fix", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out.get("dry_run") is True
    ids = {p["id"] for p in out["remediations"]}
    assert "start_daemon" in ids
    assert "retry_enabled_p" in ids
    assert "worker_offline_w-offline" in ids
    # Alias: --dry-run must not write (same proposal path).
    props = propose_remediations(tmp_path)
    assert any(p["id"] == "retry_enabled_p" for p in props)


def test_cuda_health_does_not_import_torch_unless_asked(monkeypatch) -> None:
    import importlib

    from tqdaemon import resources

    seen = []
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a: seen.append(name) or real(name, *a))
    resources.cuda_health()
    assert "torch" not in seen


def test_top_ram_consumers_respects_time_budget() -> None:
    from tqdaemon.resources import top_ram_consumers

    stats: dict = {}
    rows = top_ram_consumers(n=3, budget_sec=1e-9, stats=stats)
    assert stats.get("partial") is True and isinstance(rows, list)


def test_legacy_max_concurrent_tasks_is_flagged(tmp_path: Path) -> None:
    """Wave 4.7: max_concurrent_tasks overlaps max_running_tasks — doctor advises switching."""
    from tqdaemon.config import db_path
    from tqdaemon.db import open_db, set_config

    db = open_db(db_path(tmp_path))
    by = {c["name"]: c for c in run_checks(tmp_path)["checks"]}
    assert "limits" not in by
    set_config(db, "max_concurrent_tasks", 3)
    db.close()
    by = {c["name"]: c for c in run_checks(tmp_path)["checks"]}
    assert by["limits"]["level"] == "warn" and "max_running_tasks=3" in by["limits"]["detail"]
