"""Round 11 Wave C: row_resources, admit SELECT projection, tick order doc contract."""
from __future__ import annotations

from pathlib import Path

from taskqueue.db import open_db, parse_task_yaml, upsert_task
from taskqueue.rows import admit_task_select_sql, row_resources
from taskqueue._db_tasks import get_queued_tasks, get_running_tasks
from tests.test_queue import _project, _yaml


def test_f127_row_resources_tolerant() -> None:
    assert row_resources({"resources": '{"cpu_cores": 2}'})["cpu_cores"] == 2
    assert row_resources({"resources": {"ram_gib": 1.5}})["ram_gib"] == 1.5
    assert row_resources({"resources": "not-json"}) == {}
    assert row_resources({"resources": None}) == {}
    assert row_resources({"resources": "[]"}) == {}


def test_f128_admit_select_omits_secrets(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    sql = admit_task_select_sql(db)
    assert "env" not in sql.split("FROM")[0]
    assert "yaml_source" not in sql.split("FROM")[0]
    assert "args_override" not in sql.split("FROM")[0]
    proj = _project("p", tmp_path)
    upsert_task(
        db,
        parse_task_yaml(
            _yaml(tmp_path, "p", "t", command="x", env={"SECRET": "x"}),
            proj,
        ),
    )
    rows = get_queued_tasks(db)
    assert rows and "t" in {r["task_id"] for r in rows}
    assert "env" not in rows[0].keys()
    db.execute("UPDATE tasks SET status='running' WHERE task_id='t'")
    db.commit()
    running = get_running_tasks(db)
    assert running and "env" not in running[0].keys()
    db.close()


def test_daemon_tick_phase_order_documented() -> None:
    """Authoritative ordering assertions live in tests/test_daemon_tick.py."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "scheduler.md").read_text(
        encoding="utf-8"
    )
    assert "Actual order in `daemon_tick`" in doc
    assert "Cancel deps / scavenger / Admit" in doc
