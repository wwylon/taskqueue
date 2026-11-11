"""Wave 3 leftover: the dashboard queue payload loads only the task columns it shows."""

from __future__ import annotations

import json

from tests.helpers.master_worker import make_handler_env, seed_queued_task
from tqdaemon.web.responses import _QUEUE_SLIM_COLUMNS, _get_queue_response, _queue_row_dict


def test_slim_columns_exist_and_give_the_same_dashboard_rows(tmp_path) -> None:
    _h, db, _ = make_handler_env(tmp_path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    assert set(_QUEUE_SLIM_COLUMNS) <= cols
    for i in range(6):
        seed_queued_task(db, project="p", task_id=f"t{i}", dispatch="local")
    db.execute(
        "UPDATE tasks SET tags = ?, description = 'd', grp = 'g', depends_on = ?, "
        "resources = ?, env = '{\"SECRET\": \"x\"}', args = '[\"--big\"]' WHERE task_id = 't1'",
        (json.dumps(["gpu"]), json.dumps(["t0"]), json.dumps({"cpu_cores": 2, "gpu_vram_gib": 4})),
    )
    db.commit()
    for sort in ("status", "deps", "order", "priority"):
        full = _get_queue_response(db, project="p", sort=sort)
        slim = _get_queue_response(db, project="p", sort=sort, slim=True)
        assert [_queue_row_dict(t) for t in slim["tasks"]] == [_queue_row_dict(t) for t in full["tasks"]], sort
        assert "args" not in slim["tasks"][0] and "args" in full["tasks"][0]  # /api/queue keeps full rows
    db.close()
