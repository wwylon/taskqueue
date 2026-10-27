"""Round 3 invariant regressions (F-058–F-063, F-062, F-067)."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskqueue.config import Project
from taskqueue.daemon import _cas_master_lease
from taskqueue.yaml_sync import _sync_project, clear_yaml_mtime_cache, iter_queue_files
from taskqueue.db import open_db, set_config
from taskqueue.worker_api import claim_next_remote_task


def _project(tmp_path: Path, name: str = "p") -> Project:
    root = tmp_path / name
    qdir = root / "queue"
    qdir.mkdir(parents=True)
    return Project(name=name, root=root, queue_dir=qdir)


def _write_task_yaml(queue_dir: Path, task_id: str, *, suffix: str = ".yaml") -> Path:
    path = queue_dir / f"{task_id}{suffix}"
    path.write_text(
        f"task_id: {task_id}\ncommand: echo\nargs: [ok]\npriority: 50\nenabled: true\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# F-058
# ---------------------------------------------------------------------------

def test_same_host_daemons_do_not_share_lease_identity(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    ok1 = _cas_master_lease(db, "host:100:uuid-a", ttl=90)
    ok2 = _cas_master_lease(db, "host:101:uuid-b", ttl=90)
    assert ok1 is True
    assert ok2 is False
    row = db.execute("SELECT holder, epoch FROM master_lease WHERE singleton=1").fetchone()
    assert row["holder"] == "host:100:uuid-a"
    assert int(row["epoch"]) >= 1


def test_lease_renew_fails_for_other_incarnation(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    assert _cas_master_lease(db, "host:1:aaa", ttl=90)
    assert _cas_master_lease(db, "host:1:aaa", ttl=90) is True  # renew self
    assert _cas_master_lease(db, "host:2:bbb", ttl=90) is False


# ---------------------------------------------------------------------------
# F-060 / F-061
# ---------------------------------------------------------------------------

def test_sync_at_cap_preserves_existing_tasks(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _write_task_yaml(proj.queue_dir, "a")
    _write_task_yaml(proj.queue_dir, "b")
    db = open_db(tmp_path / "q.db")
    set_config(db, "max_queued_tasks_per_project", 2)

    # First sync admits both.
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    ids = {r["task_id"] for r in db.execute("SELECT task_id FROM tasks WHERE project='p'")}
    assert ids == {"a", "b"}

    # Cold cache + already at cap must not unregister existing YAML-backed tasks.
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    ids2 = {r["task_id"] for r in db.execute("SELECT task_id FROM tasks WHERE project='p'")}
    assert ids2 == {"a", "b"}


@pytest.mark.parametrize("suffix", [".yaml", ".yml"])
def test_sync_supports_both_yaml_suffixes(tmp_path: Path, suffix: str) -> None:
    proj = _project(tmp_path)
    _write_task_yaml(proj.queue_dir, "task1", suffix=suffix)
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    row = db.execute(
        "SELECT task_id FROM tasks WHERE project=? AND task_id=?",
        ("p", "task1"),
    ).fetchone()
    assert row is not None


def test_iter_queue_files_includes_yml(tmp_path: Path) -> None:
    q = tmp_path / "q"
    q.mkdir()
    (q / "a.yaml").write_text("x: 1\n", encoding="utf-8")
    (q / "b.yml").write_text("x: 1\n", encoding="utf-8")
    names = [p.name for p in iter_queue_files(q)]
    assert "a.yaml" in names
    assert "b.yml" in names


# ---------------------------------------------------------------------------
# F-059
# ---------------------------------------------------------------------------

def _insert_remote_task(db, project: str, task_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    res = json.dumps({"cpu_cores": 1, "ram_gib": 0.0, "gpu_vram_gib": 0.0})
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, max_attempts, enabled, enqueued_at, updated_at) "
        "VALUES (?, ?, 'queued', 'true', '[]', 'remote', 50, ?, '[]', 0, 0, 1, ?, ?)",
        (project, task_id, res, now, now),
    )
    db.commit()


def _insert_worker(db, worker_id: str, projects: list[str]):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen) "
        "VALUES (?, 'windows', 'x86_64', 16.0, 8, 0.0, '[]', ?, 'online', ?)",
        (worker_id, json.dumps(projects), now),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()


def test_two_workers_cannot_exceed_global_cap(tmp_path: Path, monkeypatch) -> None:
    """Two different tasks, two workers, max_running_tasks=1 — barrier before write lock."""
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "max_running_tasks", 1)
    _insert_remote_task(db, "p1", "t1")
    _insert_remote_task(db, "p2", "t2")
    w1 = _insert_worker(db, "w1", ["p1"])
    w2 = _insert_worker(db, "w2", ["p2"])

    barrier = threading.Barrier(2)
    import taskqueue.db as dbmod
    real_open = dbmod.open_write_conn

    def gated_open(*args, **kwargs):
        barrier.wait(timeout=10)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(dbmod, "open_write_conn", gated_open)

    results: list[dict] = [None, None]  # type: ignore

    def run(idx: int, worker) -> None:
        results[idx] = claim_next_remote_task(db_path, worker, lambda: {})

    t_a = threading.Thread(target=run, args=(0, w1))
    t_b = threading.Thread(target=run, args=(1, w2))
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)

    claimed = sum(1 for r in results if r and r.get("task") is not None)
    assert claimed == 1, results
    running = db.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status='running'"
    ).fetchone()["n"]
    assert running == 1


# ---------------------------------------------------------------------------
# F-063
# ---------------------------------------------------------------------------

def test_remote_override_consumed_in_claim_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_remote_task(db, "proj", "t")
    db.execute(
        "UPDATE tasks SET args=?, args_override=? WHERE project='proj' AND task_id='t'",
        (json.dumps(["old"]), json.dumps(["new"])),
    )
    db.commit()
    worker = _insert_worker(db, "w1", ["proj"])

    response = claim_next_remote_task(db_path, worker, lambda: {})
    assert response["task"] is not None
    assert response["task"]["args"] == ["new"]

    row = db.execute(
        "SELECT args, args_override FROM tasks WHERE project='proj' AND task_id='t'"
    ).fetchone()
    assert json.loads(row["args"]) == ["new"]
    assert row["args_override"] is None


# ---------------------------------------------------------------------------
# F-062
# ---------------------------------------------------------------------------

def test_nonloopback_standby_requires_auth(tmp_path: Path) -> None:
    from taskqueue.db import set_config
    from taskqueue.web import start_standby_server

    db = open_db(tmp_path / "queue.db")
    set_config(db, "auth_token", "")
    with pytest.raises(RuntimeError, match="auth_token"):
        start_standby_server(db, tmp_path, bind_host="0.0.0.0", port=0)


# ---------------------------------------------------------------------------
# F-067 (smoke: kwargs present on the worker spawn path)
# ---------------------------------------------------------------------------

def test_worker_popen_uses_utf8_encoding(tmp_path: Path) -> None:
    """A child printing bytes the Windows code page cannot map is read as UTF-8 with replacement."""
    import subprocess
    import sys

    from taskqueue.worker_slot import child_stdio_kwargs

    code = "import sys; sys.stdout.buffer.write('ok \\u0645 '.encode() + bytes([0x81, 10]))"
    p = subprocess.Popen([sys.executable, "-c", code], **child_stdio_kwargs())
    out, _ = p.communicate(timeout=30)
    assert out.startswith("ok م ") and "�" in out


def test_sync_invalid_yaml_sentinel_inserts_and_holds_no_lock(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj.queue_dir / "bad.yaml").write_text("task_id: bad\ncommand: a\b.exe\n", encoding="utf-8")
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    row = db.execute("SELECT status, enabled FROM tasks WHERE task_id='bad'").fetchone()
    assert (row["status"], row["enabled"]) == ("disabled", 0)
    assert not db.in_transaction


def test_run_summary_counts_runs(tmp_path: Path) -> None:
    from taskqueue.db import run_summary

    db = open_db(tmp_path / "q.db")
    assert run_summary(db, "p", "t")["run_number"] == 0
    for i in (1, 2):
        db.execute(
            "INSERT INTO runs (project, task_id, started_at, finished_at, status) "
            "VALUES ('p','t',?,?,'completed')",
            (f"2026-01-0{i}T00:00:00+00:00", f"2026-01-0{i}T00:01:00+00:00"),
        )
    db.commit()
    s = run_summary(db, "p", "t")
    assert s["run_number"] == 2
    assert s["last_run_started_at"].startswith("2026-01-02")


def test_remote_malformed_override_is_cleared_not_wedged(tmp_path: Path) -> None:
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    _insert_remote_task(db, "proj", "t")
    db.execute(
        "UPDATE tasks SET args=?, args_override=? WHERE project='proj' AND task_id='t'",
        (json.dumps(["old"]), "{not json"),
    )
    db.commit()
    worker = _insert_worker(db, "w1", ["proj"])

    claim_next_remote_task(db_path, worker, lambda: {})

    row = db.execute(
        "SELECT args_override FROM tasks WHERE project='proj' AND task_id='t'"
    ).fetchone()
    assert row["args_override"] is None


def test_parse_error_keeps_tasks_whose_id_differs_from_stem(tmp_path: Path) -> None:
    """A half-saved YAML must not delete tasks (and history) registered from that file."""
    proj = _project(tmp_path)
    path = proj.queue_dir / "m.yaml"
    path.write_text("task_id: real-id\ncommand: echo\nargs: [ok]\n", encoding="utf-8")
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    assert db.execute("SELECT 1 FROM tasks WHERE project='p' AND task_id='real-id'").fetchone()

    path.write_text("task_id: real-id\ncommand: [unclosed\n", encoding="utf-8")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    assert db.execute("SELECT 1 FROM tasks WHERE project='p' AND task_id='real-id'").fetchone()
