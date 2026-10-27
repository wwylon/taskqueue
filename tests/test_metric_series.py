"""TQ_METRIC samples: recorded per key, capped, kept out of run diffs."""
from __future__ import annotations

from taskqueue.log_issues import SERIES_MAX_KEYS, SERIES_MAX_POINTS, merge_metrics_payload, record_metric_samples


def test_numeric_samples_capped_and_reserved_key_protected():
    series: dict = {}
    for i in range(SERIES_MAX_POINTS + 10):
        record_metric_samples(series, {"loss": 1.0 / (i + 1), "phase": "train", "ok": True}, float(i))
    assert list(series) == ["loss"] and len(series["loss"]) == SERIES_MAX_POINTS
    assert series["loss"][-1][0] == float(SERIES_MAX_POINTS + 9)
    for k in range(SERIES_MAX_KEYS + 3):
        record_metric_samples(series, {f"k{k}": k}, 0.0)
    assert len(series) == SERIES_MAX_KEYS
    payload = merge_metrics_payload({"loss": 0.1, "_series": "forged", "log": 1}, None, series)
    assert payload["_series"] is not series and set(payload["_series"]) == set(series)
    assert "log" not in payload


def test_run_diff_ignores_series(tmp_path):
    import json

    from taskqueue.db import open_db
    from taskqueue.operator_features import diff_runs

    db = open_db(tmp_path / "q.db")
    for mj in ({"loss": 0.1, "_series": {"loss": [[1, 0.2]]}}, {"loss": 0.1, "_series": {"loss": [[5, 0.9]]}}):
        db.execute("INSERT INTO runs (project, task_id, started_at, status, metrics_json) VALUES ('p','t','2026-01-01','completed',?)",
                   (json.dumps(mj),))
    db.commit()
    d = diff_runs(db, 1, 2)
    assert "metrics" not in d["changes"]


def test_runner_persists_series_from_task_output(tmp_path):
    import json
    import sys

    from taskqueue.db import get_task, open_db, parse_task_yaml, upsert_task
    from taskqueue.runner import ActiveRun
    from tests.helpers.wait import wait_until
    from tests.test_queue import _project, _yaml

    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    script = tmp_path / "m.py"
    script.write_text(
        "import json\nfor i in range(3):\n    print('TQ_METRIC ' + json.dumps({'loss': 1.0 / (i + 1)}), flush=True)\n",
        encoding="utf-8",
    )
    task = parse_task_yaml(_yaml(tmp_path, "p", "m", command=sys.executable, args=[str(script)]), proj)
    upsert_task(db, task)
    run = ActiveRun(conn=db, task_row=get_task(db, "p", "m"), log_dir=tmp_path / "logs", cwd=tmp_path)
    assert run.start(use_resume=False) is None
    wait_until(run.poll, timeout=30, interval=0.05)
    run.update_db_progress_light()
    mj = json.loads(db.execute("SELECT metrics_json FROM runs WHERE run_id=?", (run.run_id,)).fetchone()[0])
    assert [p[1] for p in mj["_series"]["loss"]] == [1.0, 0.5, 1.0 / 3]
