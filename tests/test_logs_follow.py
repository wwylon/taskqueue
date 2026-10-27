from __future__ import annotations

import io

from taskqueue._cli_task import follow_log
from taskqueue.db import insert_run, open_db
from tests.helpers.master_worker import seed_queued_task


def _setup(tmp_path, status):
    home = tmp_path
    db = open_db(home / "queue.db")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    log = tmp_path / "t.log"
    log.write_text("first\n", encoding="utf-8")
    insert_run(db, "p", "t", str(log))
    db.execute("UPDATE tasks SET status=? WHERE task_id='t'", (status,))
    db.commit()
    db.close()
    return home, log


def _patch_db(monkeypatch, home):
    monkeypatch.setattr("taskqueue._cli_task._db", lambda h: home / "queue.db")


def test_follow_prints_new_output_then_exits_with_task_status(tmp_path, monkeypatch):
    home, log = _setup(tmp_path, "running")
    _patch_db(monkeypatch, home)
    ticks = []

    def fake_sleep(_):
        ticks.append(1)
        if len(ticks) == 1:
            with log.open("a", encoding="utf-8") as fh:
                fh.write("second\n")
        else:
            db = open_db(home / "queue.db")
            db.execute("UPDATE tasks SET status='completed' WHERE task_id='t'")
            db.commit()
            db.close()

    out = io.StringIO()
    assert follow_log(home, "p/t", out=out, sleep=fake_sleep, poll_sec=0) == 0
    assert out.getvalue().replace("\r\n", "\n") == "first\nsecond\n"


def test_follow_failed_task_exits_1_and_unknown_exits_3(tmp_path, monkeypatch):
    home, _ = _setup(tmp_path, "failed")
    _patch_db(monkeypatch, home)
    assert follow_log(home, "p/t", out=io.StringIO(), sleep=lambda _: None, poll_sec=0) == 1
    assert follow_log(home, "p/nope", out=io.StringIO(), sleep=lambda _: None, poll_sec=0) == 3
