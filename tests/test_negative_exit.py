"""negative_exit_codes: an expected "no" is completed (tq.run.negative), not failed; dependents choose."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from taskqueue import errors as tqerr
from taskqueue.daemon import _collect_finished_runs, is_negative_exit
from taskqueue.db import get_task, open_db, parse_task_yaml, upsert_task
from taskqueue.queries import cancel_negative_dependents
from taskqueue.runner import ActiveRun
from tests.helpers.wait import wait_until
from tests.test_queue import _project, _yaml


def test_yaml_validation(tmp_path: Path) -> None:
    proj = _project("p", tmp_path)
    ok = parse_task_yaml(_yaml(tmp_path, "p", "g", command="x", negative_exit_codes=[3, 3, 4],
                               on_dependency_negative="cancel"), proj)
    assert ok.negative_exit_codes == (3, 4) and ok.on_dependency_negative == "cancel"
    for bad in ({"negative_exit_codes": [0]}, {"negative_exit_codes": "3"}, {"on_dependency_negative": "skip"}):
        with pytest.raises(ValueError):
            parse_task_yaml(_yaml(tmp_path, "p", "b", command="x", **bad), proj)


def test_gate_exit_3_completes_negative_and_cancels_opted_in_dependent(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    gate = tmp_path / "gate.py"
    gate.write_text("raise SystemExit(3)\n", encoding="utf-8")
    upsert_task(db, parse_task_yaml(_yaml(tmp_path, "p", "gate", command=sys.executable, args=[str(gate)],
                                          negative_exit_codes=[3]), proj))
    for tid, mode in (("follow_run", "run"), ("follow_cancel", "cancel")):
        upsert_task(db, parse_task_yaml(_yaml(tmp_path, "p", tid, command="x", depends_on=["gate"],
                                              on_dependency_negative=mode), proj))
    run = ActiveRun(conn=db, task_row=get_task(db, "p", "gate"), log_dir=tmp_path / "logs", cwd=tmp_path)
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    active = {("p", "gate"): run}
    _collect_finished_runs(db, active, None)
    row = get_task(db, "p", "gate")
    assert row["status"] == "completed" and row["last_error_code"] == tqerr.RUN_NEGATIVE
    assert cancel_negative_dependents(db) == [("p", "follow_cancel")]
    assert get_task(db, "p", "follow_cancel")["status"] == "cancelled"
    assert get_task(db, "p", "follow_run")["status"] == "queued"


def test_other_nonzero_still_fails() -> None:
    row = {"negative_exit_codes": "[3]"}

    class R(dict):
        def keys(self):
            return super().keys()

    assert is_negative_exit(R(row), 3) is True
    assert is_negative_exit(R(row), 1) is False
    assert is_negative_exit(R(row), 0) is False
