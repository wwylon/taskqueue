"""Round 3 Wave C/D — F-066 CPU times, F-069 report, F-070 cache/N+1, F-071 candidates."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue.config import Project
from taskqueue.yaml_sync import _sync_project, _yaml_mtime_cache, clear_yaml_mtime_cache
from taskqueue.db import open_db
from taskqueue.runner import ActiveRun


def test_drain_cpu_seconds_from_cpu_times() -> None:
    """F-066: usage accounting uses cpu_times deltas, not declared cores."""
    run = ActiveRun.__new__(ActiveRun)
    run.accumulated_cpu_seconds = 0.0
    run._flushed_cpu_seconds = 0.0
    run._prev_cpu_times_total = None
    run._psutil_procs = {}

    class _T:
        def __init__(self, u, s):
            self.user = u
            self.system = s

    class _P:
        def __init__(self, pid, u, s):
            self.pid = pid
            self._u = u
            self._s = s

        def cpu_times(self):
            return _T(self._u, self._s)

    procs1 = [_P(1, 1.0, 0.5)]
    run._get_psutil_proc = lambda pid: procs1[0]  # type: ignore
    run._accumulate_cpu_times(procs1)
    assert run.accumulated_cpu_seconds == 0.0  # first sample establishes baseline

    procs2 = [_P(1, 2.0, 1.0)]
    run._get_psutil_proc = lambda pid: procs2[0]  # type: ignore
    run._accumulate_cpu_times(procs2)
    assert abs(run.accumulated_cpu_seconds - 1.5) < 1e-9
    assert abs(run.drain_cpu_seconds() - 1.5) < 1e-9
    assert run.drain_cpu_seconds() == 0.0


def test_cmd_report_estimate_uses_open_connection(tmp_path: Path, capsys, monkeypatch) -> None:
    """F-069: estimate_accuracy must not query a closed connection."""
    from taskqueue import _cli_admin

    home = tmp_path
    db = open_db(home / "queue.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, "
        "estimated_ram_gib, peak_ram_gib) VALUES ('p','t', ?, ?, 'completed', 2.0, 3.0)",
        (now, now),
    )
    db.commit()
    db.close()

    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    _cli_admin.cmd_report(home, project=None, days=7)
    out = capsys.readouterr().out
    assert "estimate_accuracy" in out
    assert "unavailable" not in out or "median ratio" in out


def test_yaml_cache_evicts_removed_paths(tmp_path: Path) -> None:
    """F-070: sync drops cache entries for deleted queue files."""
    root = tmp_path / "proj"
    qdir = root / "queue"
    qdir.mkdir(parents=True)
    proj = Project(name="p", root=root, queue_dir=qdir)
    a = qdir / "a.yaml"
    a.write_text("task_id: a\ncommand: echo\nargs: []\npriority: 50\n", encoding="utf-8")
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    assert str(a) in _yaml_mtime_cache
    a.unlink()
    _sync_project(db, proj)
    assert str(a) not in _yaml_mtime_cache
    db.close()


def test_claim_uses_bounded_candidate_window(tmp_path, monkeypatch) -> None:
    """F-071: a remote claim fetches a bounded candidate window, never the entire queue."""
    from taskqueue import _db_tasks
    from taskqueue.worker_api import claim_next_remote_task
    from tests.helpers.master_worker import make_handler_env, seed_queued_task, seed_worker

    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    _h, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    seed_worker(db)
    seen: list = []
    real = _db_tasks.get_queued_tasks

    def spy(conn, *a, **kw):
        seen.append(kw.get("limit"))
        return real(conn, *a, **kw)

    monkeypatch.setattr(_db_tasks, "get_queued_tasks", spy)
    worker = db.execute("SELECT * FROM workers WHERE worker_id='w1'").fetchone()
    claim_next_remote_task(tmp_path / "queue.db", worker, lambda: {"proj": None})
    assert seen and all(isinstance(n, int) and 0 < n <= 256 for n in seen)
