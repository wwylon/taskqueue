"""F-217: a DB error in the output reader thread must not kill the running task."""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

from tests.helpers.wait import wait_until
from tests.test_queue import _project, _yaml
from tqdaemon import runner as runner_mod
from tqdaemon.daemon import _collect_finished_runs
from tqdaemon.db import get_task, open_db, parse_task_yaml, upsert_task
from tqdaemon.runner import ActiveRun


def test_locked_db_during_progress_write_does_not_crash_reader(tmp_path: Path, monkeypatch) -> None:
    db_file = tmp_path / "queue.db"
    db = open_db(db_file)
    proj = _project("p", tmp_path)
    script = tmp_path / "chatty.py"
    script.write_text(
        "import time\nfor i in range(5):\n    print(f'step ({(i + 1) * 20}%)', flush=True)\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    upsert_task(db, parse_task_yaml(_yaml(tmp_path, "p", "chatty", command=sys.executable, args=[str(script)]), proj))

    main = threading.get_ident()
    real = runner_mod.update_run_progress
    failures = []

    def flaky(conn, *a, **k):
        if threading.get_ident() != main:
            failures.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real(conn, *a, **k)

    monkeypatch.setattr(runner_mod, "update_run_progress", flaky)
    run = ActiveRun(
        conn=db, task_row=get_task(db, "p", "chatty"), log_dir=tmp_path / "logs", cwd=tmp_path, db_path=db_file
    )
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    assert failures, "reader thread should have attempted (and failed) a progress write"
    assert run.reader_crashed is False
    assert run.progress_pct == 100.0
    _collect_finished_runs(db, {("p", "chatty"): run}, None)
    row = get_task(db, "p", "chatty")
    assert row["status"] == "completed"
    # The final (main-thread) flush persisted the last progress the reader could not.
    pct = db.execute("SELECT progress_pct FROM runs WHERE run_id = ?", (run.run_id,)).fetchone()[0]
    assert pct == 100.0
