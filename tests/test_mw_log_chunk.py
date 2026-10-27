"""POST /api/worker/log-chunk — streaming append and lease renew."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from taskqueue.worker_api import claim_next_remote_task
from tests.helpers.master_worker import (
    http_request,
    make_handler_env,
    seed_queued_task,
    seed_worker,
)


def _claim(db_path, worker_row):
    return claim_next_remote_task(db_path, worker_row, lambda: {"proj": None})


def test_log_chunk_appends_in_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(home / "queue.db", worker)
    run_id = claim["run_id"]

    for line in ("line one\n", "line two\n(50.0%)\n"):
        code, data = http_request(
            HandlerClass,
            "POST",
            "/api/worker/log-chunk",
            {
                "run_id": run_id,
                "worker_id": "w1",
                "session_id": "sess-1",
                "chunk_b64": base64.b64encode(line.encode()).decode("ascii"),
            },
        )
        assert code == 200
        assert data.get("ok") is True

    log_path = Path(claim["log_path"])
    text = log_path.read_text(encoding="utf-8")
    assert "line one" in text
    assert "line two" in text
    row = db.execute("SELECT progress_pct FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert float(row["progress_pct"]) == 50.0
    db.close()


def test_log_chunk_session_mismatch_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(home / "queue.db", worker)
    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/log-chunk",
        {
            "run_id": claim["run_id"],
            "worker_id": "w1",
            "session_id": "other-session",
            "chunk_b64": base64.b64encode(b"x\n").decode("ascii"),
        },
    )
    assert code == 403
    assert data.get("ok") is False
    db.close()


def test_log_chunk_renews_lease(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(home / "queue.db", worker)
    run_id = claim["run_id"]
    before = db.execute("SELECT lease_expires_at FROM runs WHERE run_id=?", (run_id,)).fetchone()[
        "lease_expires_at"
    ]
    http_request(
        HandlerClass,
        "POST",
        "/api/worker/log-chunk",
        {
            "run_id": run_id,
            "worker_id": "w1",
            "session_id": "sess-1",
            "chunk_b64": base64.b64encode(b"ping\n").decode("ascii"),
        },
    )
    after = db.execute("SELECT lease_expires_at FROM runs WHERE run_id=?", (run_id,)).fetchone()[
        "lease_expires_at"
    ]
    assert after >= before
    db.close()


def test_log_chunk_increments_metrics_json_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(home / "queue.db", worker)
    run_id = claim["run_id"]
    chunk = "WARN: timed out\nREJECT: skip\n0 errors\n(10.0%)\n"
    code, data = http_request(
        HandlerClass,
        "POST",
        "/api/worker/log-chunk",
        {
            "run_id": run_id,
            "worker_id": "w1",
            "session_id": "sess-1",
            "chunk_b64": base64.b64encode(chunk.encode()).decode("ascii"),
        },
    )
    assert code == 200
    assert data.get("ok") is True
    row = db.execute("SELECT metrics_json, progress_pct FROM runs WHERE run_id=?", (run_id,)).fetchone()
    payload = json.loads(row["metrics_json"])
    assert payload["log"]["warnings"] == 1
    assert payload["log"]["errors"] == 1
    assert payload["log"]["other"] == 0
    assert float(row["progress_pct"]) == 10.0
    db.close()


def test_log_chunk_retry_with_same_seq_is_not_duplicated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(home / "queue.db", worker)
    run_id = claim["run_id"]

    def _send(text: str, seq: int):
        return http_request(
            HandlerClass,
            "POST",
            "/api/worker/log-chunk",
            {
                "run_id": run_id,
                "worker_id": "w1",
                "session_id": "sess-1",
                "seq": seq,
                "chunk_b64": base64.b64encode(text.encode()).decode("ascii"),
            },
        )

    base = 9_000_000_000_000
    assert _send("alpha\n", base)[1].get("ok") is True
    code, data = _send("alpha\n", base)  # retried POST
    assert code == 200 and data.get("duplicate") is True
    assert _send("beta\n", base + 1)[1].get("ok") is True

    text = Path(claim["log_path"]).read_text(encoding="utf-8")
    assert text.count("alpha") == 1
    assert "beta" in text
    db.close()


def test_log_chunk_busy_db_503_then_retry_same_seq_is_applied(tmp_path: Path, monkeypatch) -> None:
    """A 503 'database busy' must not write the file or burn the seq (retry must apply)."""
    from taskqueue.db import open_write_conn

    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    HandlerClass, db, home = make_handler_env(tmp_path)
    seed_queued_task(db, dispatch="remote")
    worker = seed_worker(db, session_id="sess-1")
    claim = _claim(home / "queue.db", worker)
    run_id = claim["run_id"]
    monkeypatch.setattr(HandlerClass, "_open_write_conn", lambda self, timeout=10.0: open_write_conn(home / "queue.db", 0.05))

    body = {
        "run_id": run_id, "worker_id": "w1", "session_id": "sess-1", "seq": 9_100_000_000_000,
        "chunk_b64": base64.b64encode(b"gamma (40.0%)\n").decode("ascii"),
    }
    blocker = open_write_conn(home / "queue.db", 1.0)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        code, data = http_request(HandlerClass, "POST", "/api/worker/log-chunk", body)
    finally:
        blocker.rollback()
        blocker.close()
    assert code == 503
    assert not Path(claim["log_path"]).exists() or "gamma" not in Path(claim["log_path"]).read_text(encoding="utf-8")

    code, data = http_request(HandlerClass, "POST", "/api/worker/log-chunk", body)
    assert code == 200 and data.get("ok") is True and not data.get("duplicate")
    assert "gamma" in Path(claim["log_path"]).read_text(encoding="utf-8")
    assert float(db.execute("SELECT progress_pct FROM runs WHERE run_id=?", (run_id,)).fetchone()["progress_pct"]) == 40.0
    db.close()
