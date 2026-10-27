"""D1 CAS push: a truncated upload must not be stored or leave a .tmp behind."""

from __future__ import annotations

from pathlib import Path

from taskqueue.worker_api import claim_next_remote_task
from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task, seed_worker


def test_truncated_cas_upload_rejected_and_cleaned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = claim_next_remote_task(home / "queue.db", worker, lambda: {"proj": None})
    run_id = claim["run_id"]
    token = db.execute("SELECT finalize_token FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]

    code, data = http_request(
        HandlerClass,
        "POST",
        f"/api/data/push/{run_id}/out/x.bin",
        b"short",
        extra_headers={"Content-Length": "100", "X-TQ-Finalize-Token": token},
    )
    assert code == 400
    cas = tmp_path / "cas"
    assert not list(cas.rglob("*")) or not [p for p in cas.rglob("*") if p.is_file()]
    db.close()
