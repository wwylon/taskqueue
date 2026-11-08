"""Wave 7: adopt running tasks across a daemon restart (adopt_on_restart, taskqueue.run_wrapper)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import yaml

from taskqueue import adopt
from taskqueue._db_tasks import get_task, upsert_task
from taskqueue.config import Project
from taskqueue.daemon import _collect_finished_runs, _detach_adoptable_runs, adopt_live_runs, requeue_orphaned_running
from taskqueue.db import open_db, parse_task_yaml
from taskqueue.runner import ActiveRun
from tests.helpers.wait import wait_until


def _wrapper(log: Path, exit_file: Path, *cmd: str) -> subprocess.Popen:
    return subprocess.Popen(adopt.wrap_argv(list(cmd), log, exit_file), stdin=subprocess.DEVNULL)


def test_wrapper_records_exit_code_and_output(tmp_path: Path) -> None:
    log, ex = tmp_path / "t.log", tmp_path / "t.log.exit"
    p = _wrapper(log, ex, sys.executable, "-c", "print('hello'); raise SystemExit(3)")
    assert p.wait(30) == 3
    assert adopt.read_exit_code(ex) == 3 and "hello" in log.read_text(encoding="utf-8")
    p = _wrapper(log, ex, str(tmp_path / "no-such-program"))
    assert p.wait(30) == 127 and adopt.read_exit_code(ex) == 127
    assert "could not start" in log.read_text(encoding="utf-8")


def test_live_wrapper_checks_create_time_and_command_line(tmp_path: Path) -> None:
    import psutil

    w = _wrapper(tmp_path / "w.log", tmp_path / "w.log.exit", sys.executable, "-c", "import time; time.sleep(30)")
    plain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        wait_until(lambda: adopt.live_wrapper(w.pid, None), timeout=10, msg="wrapper never visible")
        ct = psutil.Process(w.pid).create_time()
        assert adopt.live_wrapper(w.pid, ct)
        assert not adopt.live_wrapper(w.pid, ct - 100)  # pid reused by another process
        assert not adopt.live_wrapper(plain.pid, psutil.Process(plain.pid).create_time())  # not a tq wrapper
        assert not adopt.live_wrapper(None, None)
    finally:
        for p in (w, plain):
            for c in psutil.Process(p.pid).children(recursive=True):
                c.kill()
            p.kill()
            p.wait(10)


def _env(tmp_path: Path, script: str):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "job.py").write_text(script, encoding="utf-8")
    (root / "t.yaml").write_text(
        yaml.dump({"task_id": "t", "command": sys.executable, "args": ["job.py"], "resources": {"cpu_cores": 1}}),
        encoding="utf-8",
    )
    proj = Project(name="p", root=root, queue_dir=root)
    home = tmp_path / "home"
    home.mkdir()
    db = open_db(home / "queue.db")
    upsert_task(db, parse_task_yaml(root / "t.yaml", proj))
    db.commit()
    return db, home, proj


def _start(db, home: Path, proj: Project) -> ActiveRun:
    run = ActiveRun(
        db,
        get_task(db, "p", "t"),
        log_dir=home / "logs" / "p",
        cwd=proj.root,
        graceful_stop_sec=2,
        db_path=home / "queue.db",
    )
    run.adoptable = True
    assert run.start(use_resume=False) is None
    return run


def _status(db) -> tuple[str, str, int | None]:
    t = db.execute("SELECT status FROM tasks WHERE task_id='t'").fetchone()[0]
    r = db.execute("SELECT status, exit_code FROM runs WHERE task_id='t' ORDER BY run_id DESC LIMIT 1").fetchone()
    return t, r[0], r[1]


def test_restart_adopts_a_live_run_which_then_completes(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    db, home, proj = _env(
        tmp_path,
        f"import pathlib, time\nprint('10%', flush=True)\n"
        f"while not pathlib.Path({str(gate)!r}).exists(): time.sleep(0.05)\nprint('100%', flush=True)\n",
    )
    old = _start(db, home, proj)
    wait_until(lambda: "10%" in old._log_path.read_text(encoding="utf-8"), timeout=20, msg="task never printed")
    # The old daemon exits without stopping the task (tq restart with adopt_on_restart).
    kept = _detach_adoptable_runs({("p", "t"): old})
    assert kept == [("p", "t")] and old.proc.poll() is None

    adopted = adopt_live_runs(db, home, {"p": proj})
    assert list(adopted) == [("p", "t")]
    run = adopted[("p", "t")]
    assert run.run_id == old.run_id and run.elapsed_sec() >= 0
    assert requeue_orphaned_running(db, keep_run_ids=frozenset({run.run_id})) == 0
    assert _status(db)[:2] == ("running", "running")

    gate.write_text("go", encoding="utf-8")
    wait_until(run.poll, timeout=30, msg="adopted run never finished")
    _collect_finished_runs(db, adopted, home)
    assert _status(db) == ("completed", "completed", 0)
    assert not adopt.exit_file_for(run._log_path).exists()
    log = run._log_path.read_text(encoding="utf-8")
    assert "adopted by a restarted daemon" in log and "100%" in log


def test_run_that_finished_during_the_restart_keeps_its_exit_code(tmp_path: Path) -> None:
    db, home, proj = _env(tmp_path, "raise SystemExit(5)\n")
    old = _start(db, home, proj)
    exit_file = adopt.exit_file_for(old._log_path)
    wait_until(exit_file.exists, timeout=30, msg="wrapper never wrote the exit file")
    wait_until(lambda: not adopt.live_wrapper(old.proc.pid, old._pid_create_time), timeout=10, msg="wrapper lingered")

    adopted = adopt_live_runs(db, home, {"p": proj})
    run = adopted[("p", "t")]
    wait_until(run.poll, timeout=10, msg="adopted finished run never collected")
    _collect_finished_runs(db, adopted, home)
    assert _status(db) == ("failed", "failed", 5)  # not a blind 'interrupted'


def test_dead_wrapper_without_exit_file_is_still_reaped_as_orphan(tmp_path: Path) -> None:
    db, home, proj = _env(tmp_path, "import time; time.sleep(60)\n")
    old = _start(db, home, proj)
    wait_until(lambda: adopt.live_wrapper(old.proc.pid, old._pid_create_time), timeout=10, msg="wrapper not up")
    old._terminate()  # killed hard (e.g. host reboot): no exit file
    wait_until(lambda: old.proc.poll() is not None, timeout=20, msg="kill did not land")
    adopt.exit_file_for(old._log_path).unlink(missing_ok=True)

    assert adopt_live_runs(db, home, {"p": proj}) == {}
    assert requeue_orphaned_running(db) == 1
    assert _status(db)[:2] == ("queued", "interrupted")


def test_keep_runs_marker_is_one_shot_and_expires(tmp_path: Path) -> None:
    assert adopt.consume_keep_runs(tmp_path) is False
    adopt.request_keep_runs(tmp_path)
    assert adopt.consume_keep_runs(tmp_path) is True
    assert adopt.consume_keep_runs(tmp_path) is False
    adopt.request_keep_runs(tmp_path)
    marker = next(tmp_path.iterdir())
    marker.write_text(str(int(time.time()) - 3600), encoding="ascii")  # a stale restart request
    assert adopt.consume_keep_runs(tmp_path) is False


def test_restart_keeps_adoptable_runs_and_refuses_only_for_others(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskqueue import _cli_admin

    monkeypatch.setattr(_cli_admin, "_daemon_running", lambda home: 4242)
    seen = {}
    monkeypatch.setattr(_cli_admin, "_running_runs", lambda home, **kw: seen.update(kw) or ["p/old"])
    assert _cli_admin.cmd_restart(tmp_path) == 1 and seen == {"skip_adoptable": True}
    assert "p/old" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())  # refused: no keep marker left behind

    monkeypatch.setattr(_cli_admin, "_running_runs", lambda home, **kw: [])
    stopped = []
    monkeypatch.setattr(_cli_admin, "cmd_stop_daemon", lambda home, force=False: stopped.append(force) or 1)
    assert _cli_admin.cmd_restart(tmp_path) == 1  # stop failed: no start
    assert stopped == [True] and adopt.consume_keep_runs(tmp_path) is True


def test_task_survives_the_daemon_process_exiting_and_is_adopted(tmp_path: Path) -> None:
    """The crux: closing the daemon (and its job handle) must not kill an adoptable task."""
    gate = tmp_path / "gate"
    db, home, proj = _env(
        tmp_path,
        f"import pathlib, time\nprint('started', flush=True)\n"
        f"while not pathlib.Path({str(gate)!r}).exists(): time.sleep(0.05)\nraise SystemExit(0)\n",
    )
    db.close()
    fake_daemon = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
        "from pathlib import Path\n"
        "from taskqueue.db import open_db\n"
        "from taskqueue._db_tasks import get_task\n"
        "from taskqueue.runner import ActiveRun\n"
        f"home = Path({str(home)!r}); root = Path({str(proj.root)!r})\n"
        "db = open_db(home / 'queue.db')\n"
        "run = ActiveRun(db, get_task(db, 'p', 't'), log_dir=home / 'logs' / 'p', cwd=root, db_path=home / 'queue.db')\n"
        "run.adoptable = True\n"
        "assert run.start(use_resume=False) is None\n"
        "db.commit()\n"
        "os._exit(0)  # die without stopping anything, like a crash\n"
    )
    assert subprocess.run([sys.executable, "-c", fake_daemon], timeout=60).returncode == 0
    db = open_db(home / "queue.db")
    row = db.execute("SELECT pid, pid_create_time FROM runs WHERE task_id='t'").fetchone()
    assert adopt.live_wrapper(row["pid"], row["pid_create_time"]), "task died with its daemon"

    adopted = adopt_live_runs(db, home, {"p": proj})
    gate.write_text("go", encoding="utf-8")
    run = adopted[("p", "t")]
    wait_until(run.poll, timeout=30, msg="adopted run never finished")
    _collect_finished_runs(db, adopted, home)
    assert _status(db) == ("completed", "completed", 0)
