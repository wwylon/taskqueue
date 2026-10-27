"""Capacity snapshot and YAML validate."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue.capacity import build_capacity
from taskqueue.config import Project
from taskqueue.db import open_db, parse_task_yaml
from taskqueue._db_tasks import set_started_by, upsert_task
from taskqueue.validate import validate_home, validate_file


def test_build_capacity_shape(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    cap = build_capacity(db)
    assert "protocol" in cap
    assert "master" in cap
    assert "cpu_cores_total" in cap["master"]
    assert "gpu_probe_error" in cap["master"]
    assert "gpu_probe_stale" in cap["master"]
    assert "gpu_processes" in cap["master"]
    assert "workers" in cap
    assert "running_reservations" in cap


def test_capacity_partitions_local_vs_remote(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, tags, projects, last_seen, status) "
        "VALUES ('w1', 'linux', 'x86_64', 16, 8, 0, '[]', '[]', ?, 'online')",
        (now,),
    )
    for tid, cpu, wid in (("local", 2, None), ("remote", 3, "w1")):
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, resources, enqueued_at, updated_at, enabled) "
            "VALUES ('p', ?, 'running', 'true', ?, ?, ?, 1)",
            (tid, f'{{"cpu_cores": {cpu}, "ram_gib": 0, "gpu_vram_gib": 0}}', now, now),
        )
        db.execute(
            "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id) "
            "VALUES ('p', ?, ?, 'running', 'x.log', ?)",
            (tid, now, wid),
        )
    db.commit()
    cap = build_capacity(db, {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100})
    # Master should reserve only the local task (2 cores), not the remote 3.
    assert cap["master"]["cpu_cores_free"] == round(
        cap["master"]["cpu_cores_total"] - 2, 2
    )
    w = next(x for x in cap["workers"] if x["worker_id"] == "w1")
    assert w["cpu_cores_free"] == 5.0  # 8 - 3


def test_validate_ok_and_bad(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)
    (q / "good.yaml").write_text(
        yaml.dump({
            "task_id": "good",
            "command": sys.executable,
            "args": ["-c", "pass"],
        }),
        encoding="utf-8",
    )
    (q / "bad.yaml").write_text("task_id: bad/slash\ncommand: python\n", encoding="utf-8")
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"root": str(root)}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    from taskqueue.config import state_home
    report = validate_home(state_home())
    assert report["ok"] >= 1
    assert report["valid"] is False
    assert any("slash" in e["error"].lower() or "/" in e["error"] for e in report["errors"])


def test_started_by_column(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({"task_id": "t", "command": sys.executable, "args": ["-c", "pass"]}),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))
    set_started_by(db, "p", "t", "alice")
    row = db.execute("SELECT started_by FROM tasks WHERE task_id='t'").fetchone()
    assert row["started_by"] == "alice"


def test_w619_cap_warning_fires(tmp_path: Path, monkeypatch) -> None:
    """W6.19: validate warns when wall_clock_cap_sec >> historical median."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)

    # Task with a cap far above its historical runtime
    yaml_file = q / "long_cap.yaml"
    yaml_file.write_text(
        yaml.dump({
            "task_id": "long_cap",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "wall_clock_cap_sec": 9000,  # 2.5 hours
        }),
        encoding="utf-8",
    )
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"root": str(root)}}}),
        encoding="utf-8",
    )

    # Seed DB with completed runs that had ~60 s median
    from taskqueue.db import open_db as _open_db
    db_file = home / "queue.db"
    db = _open_db(db_file)
    now_start = "2026-01-01T00:00:00+00:00"
    now_end = "2026-01-01T00:01:00+00:00"  # 60 s later
    db.executemany(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at) VALUES (?,?,?,?,?)",
        [("demo", "long_cap", "completed", now_start, now_end) for _ in range(5)],
    )
    db.commit()
    db.close()

    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    from taskqueue.config import state_home
    report = validate_home(state_home())
    assert any("long_cap" in w.get("task_id", "") for w in report["warnings"]), (
        f"Expected W6.19 warning, got: {report['warnings']}"
    )
    assert report["valid"]  # warnings don't fail validation


def test_w619_cap_warning_silent_no_history(tmp_path: Path, monkeypatch) -> None:
    """W6.19: no warning emitted when there is no run history."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)

    yaml_file = q / "no_hist.yaml"
    yaml_file.write_text(
        yaml.dump({
            "task_id": "no_hist",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "wall_clock_cap_sec": 9000,
        }),
        encoding="utf-8",
    )
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"root": str(root)}}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    from taskqueue.config import state_home
    report = validate_home(state_home())
    assert report["warnings"] == []
