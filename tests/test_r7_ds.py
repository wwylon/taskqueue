"""Round 7 DS review regressions (F-079–F-085)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from taskqueue.config import Project
from taskqueue.daemon import MasterLeaseLockTimeout, _cas_master_lease, requeue_orphaned_running
from taskqueue.yaml_sync import _sync_project, clear_yaml_mtime_cache
from taskqueue.db import open_db
from taskqueue.runner import _descendant_pids, build_process_children_map
from taskqueue.scheduler import check_services_cached, clear_service_probe_cache
from taskqueue.worker_api import claim_next_remote_task


def _project(tmp_path: Path, name: str = "p") -> Project:
    root = tmp_path / name
    qdir = root / "queue"
    qdir.mkdir(parents=True)
    return Project(name=name, root=root, queue_dir=qdir)


def test_f079_vanished_queue_dir_does_not_wipe_tasks(tmp_path: Path, monkeypatch) -> None:
    proj = _project(tmp_path)
    path = proj.queue_dir / "keep.yaml"
    path.write_text(
        "task_id: keep\ncommand: echo\nargs: [ok]\npriority: 50\nenabled: true\n",
        encoding="utf-8",
    )
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    assert db.execute(
        "SELECT task_id FROM tasks WHERE project='p' AND task_id='keep'"
    ).fetchone() is not None

    listed = {"done": False}

    def _empty_list(_queue_dir):
        listed["done"] = True
        return []

    monkeypatch.setattr("taskqueue.yaml_sync.iter_queue_files", _empty_list)

    qdir = proj.queue_dir.resolve()
    real_is_dir = Path.is_dir

    def _is_dir(self: Path) -> bool:
        try:
            if self.resolve() == qdir:
                # Entry check True; after listing (empty), report vanished.
                return not listed["done"]
        except Exception:
            pass
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)
    _sync_project(db, proj)
    row = db.execute(
        "SELECT task_id FROM tasks WHERE project='p' AND task_id='keep'"
    ).fetchone()
    assert row is not None
    db.close()


def test_f080_db_flip_before_kill(tmp_path: Path, monkeypatch) -> None:
    db = open_db(tmp_path / "q.db")
    db.execute(
        "INSERT INTO tasks (project, task_id, status, enabled, command, enqueued_at, updated_at) "
        "VALUES ('p','t','running',1,'echo','2020-01-01T00:00:00+00:00','2020-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, pid, worker_id) "
        "VALUES ('p','t','2020-01-01T00:00:00+00:00','running',4242,NULL)"
    )
    db.commit()
    seen: list[str] = []

    def _kill(pid, create_time=None, graceful_sec=5.0):
        row = db.execute(
            "SELECT status FROM runs WHERE project='p' AND task_id='t'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "interrupted"
        seen.append("kill")
        return "ok"

    monkeypatch.setattr("taskqueue.reap.kill_process_tree", _kill)
    requeue_orphaned_running(db, reason="orphan", graceful_sec=0.0)
    assert seen == ["kill"]
    db.close()


@pytest.mark.slow
def test_f081_descendant_walk_and_snapshot() -> None:
    cmap = {1: [2, 3], 2: [4], 10: [11]}
    desc = _descendant_pids(1, cmap)
    assert set(desc) == {2, 3, 4}
    snap = build_process_children_map()
    assert isinstance(snap, dict)


def test_f082_cas_lock_timeout_distinct(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")

    class Busy:
        def execute(self, sql, *args):
            if "BEGIN IMMEDIATE" in sql:
                raise sqlite3.OperationalError("database is locked")
            return db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(db, name)

    with pytest.raises(MasterLeaseLockTimeout):
        _cas_master_lease(Busy(), "host:1:abc")  # type: ignore[arg-type]
    db.close()


def test_f085_claim_uses_service_cache(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake(services_json, *, allow_remote=False, system_info=None):
        calls["n"] += 1
        return ("down", "tq.block.service_down")

    monkeypatch.setattr("taskqueue.scheduler._check_services", _fake)
    clear_service_probe_cache()
    assert check_services_cached('[{"health_url":"http://127.0.0.1:9"}]')[0] == "down"
    assert check_services_cached('[{"health_url":"http://127.0.0.1:9"}]')[0] == "down"
    assert calls["n"] == 1
    clear_service_probe_cache()


def test_f084_capture_before_begin_immediate(tmp_path: Path, monkeypatch) -> None:
    """F-084: the (slow) code-package capture runs before the claim takes the write lock."""
    import sqlite3

    from taskqueue import worker_api
    from tests.helpers.master_worker import make_handler_env, seed_queued_task, seed_worker

    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    _h, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    seed_worker(db)
    lock_free: list[bool] = []
    real = worker_api._capture_code_package

    def spy(*a, **kw):
        probe = sqlite3.connect(str(tmp_path / "queue.db"), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")  # fails if the claim already holds the write lock
            probe.rollback()
            lock_free.append(True)
        except sqlite3.OperationalError:
            lock_free.append(False)
        finally:
            probe.close()
        return real(*a, **kw)

    monkeypatch.setattr(worker_api, "_capture_code_package", spy)
    worker = db.execute("SELECT * FROM workers WHERE worker_id='w1'").fetchone()
    claim = worker_api.claim_next_remote_task(tmp_path / "queue.db", worker, lambda: {"proj": None})
    assert claim.get("task") is not None
    assert lock_free == [True]


def test_p6_hold_credit_only_for_held_task() -> None:
    # P6 hold credit must only apply to the exact held task, not to any other task.
    # _cross_project_hold was removed: cross-project tasks no longer receive P6 credit.
    from taskqueue.scheduler import p6_hold_credit

    held = ("p", "big")
    assert p6_hold_credit(held, held, 12.0, 4.0) == (12.0, 4.0)
    assert p6_hold_credit(("p", "small"), held, 12.0, 4.0) == (0.0, 0.0)      # same project
    assert p6_hold_credit(("other", "big"), held, 12.0, 4.0) == (0.0, 0.0)    # cross-project
    assert p6_hold_credit(held, None, 12.0, 4.0) == (0.0, 0.0)                # no hold


def test_cas_lease_non_lock_error_not_masked(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")

    class Nested:
        def execute(self, sql, *args):
            if "BEGIN IMMEDIATE" in sql:
                raise sqlite3.OperationalError(
                    "cannot start a transaction within a transaction"
                )
            return db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(db, name)

    with pytest.raises(sqlite3.OperationalError) as ei:
        _cas_master_lease(Nested(), "host:1:abc")  # type: ignore[arg-type]
    assert not isinstance(ei.value, MasterLeaseLockTimeout)
    db.close()
