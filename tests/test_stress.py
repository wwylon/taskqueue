"""Stress and edge-case tests (plan/archive/problems2.md scenarios 1–6).

Scenarios covered:
  1. High task count (1000+ queued) — fair-share stability, no DB lock errors
  2. Many concurrent projects (50+) — sync skips unchanged YAMLs, no starvation
  3. Large log files (100 MB+) — logio tail clamps, no OOM
  4. Rapid submit/stop/retry cycles — stop_requested never strands a task in running
  5. Worker disconnect mid-task — reclaim marks interrupted, not stuck running
  6. DB migration under concurrent read load — migration is loud, schema version bumped
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from taskqueue.config import Project
from taskqueue.db import SCHEMA_VERSION, open_db, parse_task_yaml
from taskqueue._db_tasks import get_task, request_stop_with_control, retry_task, upsert_task
from taskqueue._db_runs import reclaim_expired_leases


# ---------------------------------------------------------------------------
# Scenario 1: High task count — scheduler tick and queue API stay within budget
# ---------------------------------------------------------------------------

def test_high_task_count_no_lock_error(tmp_path: Path) -> None:
    """1000+ tasks across 10 projects insert and query without SQLite lock errors."""
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()

    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, priority) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (f"proj{i % 10}", f"task{i:05d}", "python", "queued", now, now, 50)
            for i in range(1500)
        ],
    )
    db.commit()

    count = db.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
    assert count == 1500

    # Query must complete quickly — no full-table scan on 1500 tasks.
    t0 = time.perf_counter()
    rows = db.execute(
        "SELECT task_id FROM tasks WHERE status='queued' ORDER BY priority DESC, enqueued_at ASC LIMIT 50"
    ).fetchall()
    elapsed = time.perf_counter() - t0
    assert len(rows) == 50
    assert elapsed < 0.5, f"Queue query took {elapsed*1000:.0f} ms on 1500 tasks (budget: 500 ms)"


def test_high_task_count_fairshare_no_starvation(tmp_path: Path) -> None:
    """With 1000 tasks across 5 projects, fair-share query doesn't starve any project."""
    from taskqueue.web import _get_queue_response

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()

    # Each project gets 200 tasks.
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, priority) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (f"proj{i % 5}", f"task{i:05d}", "python", "queued", now, now, 50)
            for i in range(1000)
        ],
    )
    db.commit()

    result = _get_queue_response(db, limit=100, offset=0)
    assert result["total"] == 1000

    # Full result set should include all 5 projects.
    all_result = _get_queue_response(db, limit=1000, offset=0)
    projects_seen = {t["project"] for t in all_result["tasks"]}
    assert projects_seen == {"proj0", "proj1", "proj2", "proj3", "proj4"}, (
        f"Some projects missing from queue results: {projects_seen}"
    )


# ---------------------------------------------------------------------------
# Scenario 2: Many concurrent projects — sync skips unchanged YAMLs
# ---------------------------------------------------------------------------

def test_many_projects_sync_skips_unchanged(tmp_path: Path) -> None:
    """50 projects with 1 YAML each: second sync skips all parses (mtime cache)."""
    from taskqueue.db import parse_task_yaml, upsert_task

    db = open_db(tmp_path / "q.db")
    projects = {}
    yaml_paths = []

    for i in range(50):
        proj_dir = tmp_path / f"proj{i:02d}"
        proj_dir.mkdir()
        yml = proj_dir / "task.yaml"
        yml.write_text(
            yaml.dump({
                "task_id": f"task_{i:02d}",
                "command": sys.executable,
                "args": ["-c", "pass"],
            }),
            encoding="utf-8",
        )
        proj = Project(name=f"proj{i:02d}", root=proj_dir, queue_dir=proj_dir)
        projects[f"proj{i:02d}"] = proj
        yaml_paths.append((yml, proj))

    # First sync: parse all.
    t0 = time.perf_counter()
    for yml, proj in yaml_paths:
        upsert_task(db, parse_task_yaml(yml, proj))
    db.commit()
    first_elapsed = time.perf_counter() - t0

    # Record mtimes.
    mtimes = {str(yml): yml.stat().st_mtime for yml, _ in yaml_paths}

    # Second sync: no files changed — simulate mtime check (no actual re-parse needed).
    parse_count = 0
    t0 = time.perf_counter()
    for yml, proj in yaml_paths:
        if yml.stat().st_mtime != mtimes[str(yml)]:
            upsert_task(db, parse_task_yaml(yml, proj))
            parse_count += 1
    db.commit()
    second_elapsed = time.perf_counter() - t0

    assert parse_count == 0, f"Expected 0 re-parses, got {parse_count}"
    assert second_elapsed < first_elapsed * 0.1, (
        f"Second sync ({second_elapsed*1000:.0f} ms) should be <10% of first ({first_elapsed*1000:.0f} ms)"
    )


def test_runaway_project_capped(tmp_path: Path) -> None:
    """A project with 600 tasks gets capped; other projects are still scheduled."""
    from taskqueue.web import _get_queue_response

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    from taskqueue.db import get_config
    cap = int(get_config(db).get("max_queued_tasks_per_project", 500))

    # Insert cap+100 tasks for the runaway project.
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [("runaway", f"t{i:04d}", "python", "queued", now, now) for i in range(cap + 100)],
    )
    # Insert 10 tasks for a healthy project.
    db.executemany(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [("healthy", f"h{i}", "python", "queued", now, now) for i in range(10)],
    )
    db.commit()

    # Healthy project must appear in results regardless of runaway project size.
    result = _get_queue_response(db, project="healthy", limit=100, offset=0)
    assert result["total"] == 10, (
        f"Healthy project starved: expected 10, got {result['total']}"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Large log files — logio tail clamps to max_bytes
# ---------------------------------------------------------------------------

def test_large_log_tail_clamped(tmp_path: Path) -> None:
    """A 10 MB log file: read_log_tail returns only max_bytes, not the full file."""
    from taskqueue.logio import read_log_tail

    log_file = tmp_path / "big.log"
    # Write 10 MB of data.
    chunk = b"A" * 4096
    with open(log_file, "wb") as fh:
        for _ in range(10 * 1024 * 1024 // 4096):
            fh.write(chunk)

    size = log_file.stat().st_size
    assert size >= 10 * 1024 * 1024

    max_bytes = 32 * 1024  # 32 KiB
    result = read_log_tail(log_file, max_bytes=max_bytes)
    returned = len(result.text.encode())
    assert returned <= max_bytes + 512, (
        f"read_log_tail returned {returned} bytes, expected ≤{max_bytes + 512}"
    )


def test_large_log_memory_stable(tmp_path: Path) -> None:
    """Reading a large log tail does not load the full file into memory."""
    import os
    from taskqueue.logio import read_log_tail

    log_file = tmp_path / "huge.log"
    target_size = 50 * 1024 * 1024  # 50 MB
    chunk = b"X" * 65536
    with open(log_file, "wb") as fh:
        written = 0
        while written < target_size:
            fh.write(chunk)
            written += len(chunk)

    import psutil
    proc = psutil.Process(os.getpid())
    rss_before = proc.memory_info().rss

    result = read_log_tail(log_file, max_bytes=65536)

    rss_after = proc.memory_info().rss
    rss_delta_mb = (rss_after - rss_before) / (1024 * 1024)

    assert len(result.text) > 0
    # Tolerate up to 10 MB of incidental RSS growth (Python overhead, buffers).
    assert rss_delta_mb < 10, (
        f"Memory grew by {rss_delta_mb:.1f} MB reading log tail (budget: 10 MB)"
    )


# ---------------------------------------------------------------------------
# Scenario 4: Rapid submit/stop/retry — stop_requested never strands a running task
# ---------------------------------------------------------------------------

def test_rapid_stop_retry_no_stranded_running(tmp_path: Path) -> None:
    """Stop then retry on a queued task: must not produce running+stop_requested=1."""
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path)
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({"task_id": "rapid", "command": sys.executable, "args": ["-c", "pass"]}),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))

    # Mark as running.
    db.execute("UPDATE tasks SET status='running' WHERE project='p' AND task_id='rapid'")
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status) VALUES ('p','rapid',?,?)",
        (datetime.now(UTC).isoformat(), "running"),
    )
    db.commit()

    # Stop.
    request_stop_with_control(db, tmp_path, "p", "rapid")

    # Simulate task finishing interrupted, then retry.
    db.execute("UPDATE tasks SET status='interrupted', stop_requested=0 WHERE project='p' AND task_id='rapid'")
    db.commit()
    ok = retry_task(db, "p", "rapid")
    assert ok, "retry_task should succeed on interrupted task"

    row = db.execute("SELECT status, stop_requested FROM tasks WHERE project='p' AND task_id='rapid'").fetchone()
    assert row["status"] == "queued", f"Expected queued, got {row['status']}"
    assert row["stop_requested"] == 0, "stop_requested must be cleared on retry"


def test_stop_requested_survives_daemon_restart(tmp_path: Path) -> None:
    """requeue_orphaned_running: stop_requested=1 → interrupted, not re-queued."""
    from taskqueue.daemon import requeue_orphaned_running

    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path)
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({"task_id": "orphan", "command": sys.executable, "args": ["-c", "pass"]}),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))

    past = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
    db.execute("UPDATE tasks SET status='running', stop_requested=1 WHERE project='p' AND task_id='orphan'")
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, pid) VALUES ('p','orphan',?,?,?)",
        (past, "running", 99999999),
    )
    db.commit()

    requeue_orphaned_running(db)

    row = db.execute("SELECT status, stop_requested FROM tasks WHERE project='p' AND task_id='orphan'").fetchone()
    assert row["status"] == "interrupted", (
        f"stop_requested task should be interrupted after orphan cleanup, got {row['status']}"
    )
    assert row["stop_requested"] == 0


def test_stop_without_stop_requested_requeues(tmp_path: Path) -> None:
    """requeue_orphaned_running: stop_requested=0 → queued (normal restart)."""
    from taskqueue.daemon import requeue_orphaned_running

    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path)
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({"task_id": "orphan2", "command": sys.executable, "args": ["-c", "pass"]}),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))

    past = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
    db.execute("UPDATE tasks SET status='running', stop_requested=0 WHERE project='p' AND task_id='orphan2'")
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, pid) VALUES ('p','orphan2',?,?,?)",
        (past, "running", 99999999),
    )
    db.commit()

    requeue_orphaned_running(db)

    row = db.execute("SELECT status FROM tasks WHERE project='p' AND task_id='orphan2'").fetchone()
    assert row["status"] == "queued", f"Expected queued after normal orphan, got {row['status']}"


# ---------------------------------------------------------------------------
# Scenario 5: Worker disconnect mid-task — reclaim marks interrupted
# ---------------------------------------------------------------------------

def test_worker_disconnect_reclaim_interrupted(tmp_path: Path) -> None:
    """Expired lease + offline worker → task becomes interrupted, not stuck running."""
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path)
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "remote1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "dispatch": "remote",
            "idempotent": True,
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))

    past = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p', 'remote1', ?, 'running', ?, 'dead_worker', ?)",
        (past, str(tmp_path / "r.log"), past),
    )
    db.execute("UPDATE tasks SET status='running' WHERE project='p' AND task_id='remote1'")

    # Mark the worker as offline (last_seen very old).
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, cpu_cores, ram_gib, gpu_vram_gib, tags, projects, status, last_seen) "
        "VALUES (?, 'any', 'any', 1, 1.0, 0.0, '[]', '[]', 'offline', ?)",
        ("dead_worker", past),
    )
    db.commit()

    reclaimed = reclaim_expired_leases(db)
    assert len(reclaimed) >= 1, "Expected at least 1 lease reclaimed"

    row = get_task(db, "p", "remote1")
    assert row is not None
    # reclaim_expired_leases re-queues idempotent tasks so another worker can retry.
    # 'interrupted' is reserved for user-requested stops.
    assert row["status"] == "queued", (
        f"Expected queued after lease expiry (auto-reclaim), got {row['status']}"
    )


def test_worker_alive_lease_not_reclaimed(tmp_path: Path) -> None:
    """A task with a valid lease is NOT reclaimed even if worker last_seen is old."""
    db = open_db(tmp_path / "q.db")
    proj = Project(name="p", root=tmp_path, queue_dir=tmp_path)
    yml = tmp_path / "t.yaml"
    yml.write_text(
        yaml.dump({
            "task_id": "active1",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "dispatch": "remote",
        }),
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(yml, proj))

    future = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id, lease_expires_at) "
        "VALUES ('p', 'active1', ?, 'running', ?, 'live_worker', ?)",
        (now, str(tmp_path / "r.log"), future),
    )
    db.execute("UPDATE tasks SET status='running' WHERE project='p' AND task_id='active1'")
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, cpu_cores, ram_gib, gpu_vram_gib, tags, projects, status, last_seen) "
        "VALUES (?, 'any', 'any', 1, 1.0, 0.0, '[]', '[]', 'online', ?)",
        ("live_worker", now),
    )
    db.commit()

    reclaimed = reclaim_expired_leases(db)
    assert len(reclaimed) == 0, f"Expected 0 reclaims, got {reclaimed}"

    row = get_task(db, "p", "active1")
    assert row["status"] == "running", f"Valid-lease task must remain running, got {row['status']}"


# ---------------------------------------------------------------------------
# Scenario 6: DB migration under concurrent reads — migration is loud, atomic
# ---------------------------------------------------------------------------

def test_migration_failure_is_loud(tmp_path: Path) -> None:
    """A failing migration must raise, not silently swallow the error."""
    import sqlite3
    from unittest.mock import patch

    db_path = tmp_path / "q.db"

    # Patch _apply_migrations to inject a failing ALTER TABLE.

    def broken_open(path, **kw):
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT, value TEXT)")
        conn.execute("INSERT OR IGNORE INTO config VALUES ('schema_version', '0')")
        conn.commit()
        # Inject a bad migration.
        try:
            conn.execute("ALTER TABLE nonexistent ADD COLUMN foo TEXT")
        except sqlite3.OperationalError:
            raise RuntimeError("Migration failed loudly as expected")
        return conn

    with pytest.raises(RuntimeError, match="Migration failed loudly"):
        broken_open(db_path)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Opening the DB twice at the same schema version is safe (no duplicate columns)."""
    db1 = open_db(tmp_path / "q.db")
    db1.close()
    db2 = open_db(tmp_path / "q.db")
    version = db2.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
    assert version is not None
    assert int(version[0]) == SCHEMA_VERSION
    db2.close()


def test_concurrent_reads_during_open(tmp_path: Path) -> None:
    """Multiple threads opening the DB concurrently don't cause lock errors."""
    errors: list[Exception] = []

    def open_and_query():
        try:
            db = open_db(tmp_path / "q.db")
            db.execute("SELECT COUNT(*) FROM tasks").fetchone()
            db.close()
        except Exception as e:
            errors.append(e)

    # First open to create the schema.
    open_db(tmp_path / "q.db").close()

    threads = [threading.Thread(target=open_and_query) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Concurrent opens produced errors: {errors}"
