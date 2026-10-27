"""Shared helpers behind retry_task / retry batch and the excess-running stops."""
from __future__ import annotations

from taskqueue import scheduler
from taskqueue._db_tasks import _requeue_row
from taskqueue.db import open_db
from tests.helpers.master_worker import seed_queued_task


def _status(db, tid):
    return db.execute("SELECT status, enabled FROM tasks WHERE task_id=?", (tid,)).fetchone()


def test_requeue_row_respects_enabled_unless_forced(tmp_path):
    db = open_db(tmp_path / "q.db")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    db.execute("UPDATE tasks SET status='failed', enabled=0 WHERE task_id='t'")
    db.commit()
    assert _requeue_row(db, "p", "t", require_enabled=True) is False
    assert _status(db, "t")["status"] == "failed"
    assert _requeue_row(db, "p", "t", require_enabled=False) is True
    row = _status(db, "t")
    assert (row["status"], row["enabled"]) == ("queued", 1)


def test_stop_excess_stops_only_up_to_excess_and_survives_errors(monkeypatch, capsys):
    import taskqueue.db as db

    calls = []

    def stop(conn, home, p, t):
        calls.append((p, t))
        if t == "2":
            raise RuntimeError("x")

    monkeypatch.setattr(db, "request_stop_with_control", stop)
    rows = [{"project": "a", "task_id": str(i)} for i in (1, 2, 3, 4)]
    n = scheduler._stop_excess(
        None, None, rows, 2, lambda p, t, left: f"stopping {p}/{t} left={left}", "excess-stop"
    )
    assert calls == [("a", "1"), ("a", "2"), ("a", "3")] and n == 2
    err = capsys.readouterr().err
    assert "stopping a/1 left=2" in err and "warn: excess-stop a/2" in err


def test_upsert_columns_match_params_and_schema(tmp_path):
    from tests.test_cap_auto_retry import _project, _write_task
    from taskqueue import _db_tasks as dt
    from taskqueue.db import parse_task_yaml

    db = open_db(tmp_path / "q.db")
    task = parse_task_yaml(_write_task(tmp_path, "t1"), _project("p", tmp_path))
    assert len(dt._UPSERT_TASK_COLUMNS) == len(dt._upsert_task_params(task))
    cols = {r["name"] for r in db.execute("PRAGMA table_info(tasks)").fetchall()}
    assert set(dt._UPSERT_TASK_COLUMNS) <= cols
    dt.upsert_task(db, task)
    dt.upsert_task(db, task)  # conflict path
    n = db.execute("SELECT COUNT(*) FROM tasks WHERE task_id='t1'").fetchone()[0]
    assert n == 1


def test_taskspec_helpers_validate(tmp_path):
    import pytest
    from pathlib import Path
    from taskqueue import taskspec as ts

    p = Path("t.yaml")
    assert ts._validated_task_id({"task_id": "a"}, p) == "a"
    for bad in ("a/b", "x.", "CON"):
        with pytest.raises(ValueError):
            ts._validated_task_id({"task_id": bad}, p)
    assert ts._parse_resources({"resources": {"cpu_cores": 1.2, "ram_gib": 2}}, p).cpu_cores == 2
    with pytest.raises(ValueError):
        ts._parse_resources({"resources": []}, p)
    assert ts._parse_dependency_policy({"negative_exit_codes": [3, 3, 2]}, p) == ("block", (2, 3), "run")
    with pytest.raises(ValueError):
        ts._parse_dependency_policy({"negative_exit_codes": [0]}, p)
    assert ts._parse_class_and_trust({"class": "scavenger"}, p, "checkpointable") == ("scavenger", "")
    with pytest.raises(ValueError):
        ts._parse_class_and_trust({"class": "scavenger"}, p, "at_most_once")
    assert ts._parse_semantics({}, p)[1] is False


def test_handler_query_parsed_once_per_path(monkeypatch):
    from taskqueue.web import handler as h

    calls = []
    real = h.parse_qs
    monkeypatch.setattr(h, "parse_qs", lambda q, *a, **k: calls.append(q) or real(q, *a, **k))

    class Fake:
        path = "/api/x?project=p&wait=3"
        _query = h.Handler._query

    f = Fake()
    assert f._query() == {"project": ["p"], "wait": ["3"]}
    assert f._query()["project"] == ["p"]
    assert len(calls) == 1
    f.path = "/api/y?a=1"  # keep-alive: next request on the same handler object
    assert f._query() == {"a": ["1"]}
    assert len(calls) == 2


def test_restartability_levels(tmp_path):
    from taskqueue.victim import _restartability

    db = open_db(tmp_path / "q.db")
    seed_queued_task(db, project="p", task_id="idem", dispatch="local")
    db.execute("UPDATE tasks SET idempotent=1 WHERE task_id='idem'")
    seed_queued_task(db, project="p", task_id="res", dispatch="local")
    db.execute("UPDATE tasks SET resume_args='[\"--r\"]' WHERE task_id='res'")
    seed_queued_task(db, project="p", task_id="plain", dispatch="local")
    db.commit()
    def row(t):
        return db.execute("SELECT * FROM tasks WHERE task_id=?", (t,)).fetchone()
    assert _restartability(row("idem")) == 1.0
    assert _restartability(row("res")) == 0.6
    assert _restartability(row("plain")) == 0.0
    assert _restartability(None) == 0.0


def test_get_last_runs_batch_matches_single_lookup(tmp_path):
    from taskqueue.db import failed_attempt_count, get_last_run_for_task, insert_run
    from taskqueue._db_runs import get_last_runs_for_tasks

    db = open_db(tmp_path / "q.db")
    for t in ("a", "b", "c"):
        seed_queued_task(db, project="p", task_id=t, dispatch="local")
    insert_run(db, "p", "a", "l1")
    last_a = insert_run(db, "p", "a", "l2")
    insert_run(db, "p", "b", "l3")
    keys = [("p", "a"), ("p", "b"), ("p", "c"), ("p", "missing")]
    got = get_last_runs_for_tasks(db, keys)
    assert set(got) == {("p", "a"), ("p", "b")}
    assert got[("p", "a")]["run_id"] == last_a
    for k in got:
        assert got[k]["run_id"] == get_last_run_for_task(db, *k)["run_id"]
    assert get_last_runs_for_tasks(db, []) == {}
    # baseline shortcut agrees with the DB lookup
    db.execute("UPDATE runs SET status='failed' WHERE task_id='a'")
    db.execute("UPDATE tasks SET retry_attempt_baseline=1 WHERE task_id='a'")
    db.commit()
    assert failed_attempt_count(db, "p", "a") == 1
    assert failed_attempt_count(db, "p", "a", baseline=1) == 1
    assert failed_attempt_count(db, "p", "a", baseline=0) == 2
