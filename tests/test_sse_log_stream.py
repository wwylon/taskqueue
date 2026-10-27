"""Behavior tests for GET /api/logs/<id>/stream (connection cap, cleanup, content)."""
from __future__ import annotations

from pathlib import Path

from taskqueue._db_runs import insert_run
from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task


def _slots(handler_cls) -> int:
    return handler_cls._sse_semaphore._value


def test_stream_rejects_when_connection_cap_exhausted(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    held = 0
    while H._sse_semaphore.acquire(blocking=False):
        held += 1
    try:
        status, _ = http_request(H, "GET", "/api/logs/1/stream")
        assert status == 503
    finally:
        for _ in range(held):
            H._sse_semaphore.release()
    db.close()


def test_stream_unknown_run_is_404_and_releases_slot(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    before = _slots(H)
    status, _ = http_request(H, "GET", "/api/logs/9999/stream")
    assert status == 404
    assert _slots(H) == before
    db.close()


def test_stream_finished_run_sends_log_then_closes(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    log = tmp_path / "run.log"
    log.write_text("hello\nworld\n", encoding="utf-8")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    run_id = insert_run(db, "p", "t", str(log))
    db.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
    db.commit()
    before = _slots(H)

    status, body = http_request(H, "GET", f"/api/logs/{run_id}/stream")

    assert status == 200
    text = body.decode() if isinstance(body, bytes) else str(body)
    assert "data: hello\n" in text and "data: world\n" in text
    assert _slots(H) == before
    db.close()


def test_stream_resumes_from_last_event_id_and_keeps_utf8_and_partial_lines(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    log = tmp_path / "run.log"
    first = "old line\n".encode("utf-8")
    log.write_bytes(first + "żółw\r\ntail-no-newline".encode("utf-8"))
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    run_id = insert_run(db, "p", "t", str(log))
    db.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
    db.commit()

    status, body = http_request(
        H, "GET", f"/api/logs/{run_id}/stream", extra_headers={"Last-Event-ID": str(len(first))}
    )

    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    assert status == 200
    assert "retry: 3000" in text
    assert "old line" not in text  # resumed after the given byte offset, no tail replay
    assert "data: żółw\n" in text  # CRLF stripped, multi-byte chars intact
    assert "data: tail-no-newline\n" in text  # partial last line flushed when the run is over
    assert "id: " in text
    db.close()


def test_quiet_running_stream_sends_keepalive_and_frees_slot_when_client_is_gone(tmp_path: Path, monkeypatch) -> None:
    """A quiet log on a live run sends `: ka` comments; a dead socket ends the stream and frees the slot."""
    import io

    from taskqueue.web import sse

    H, db, _ = make_handler_env(tmp_path)
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    run_id = insert_run(db, "p", "t", str(log))
    db.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
    db.commit()
    monkeypatch.setattr(sse, "SSE_KEEPALIVE_SEC", 0.0)

    class Closing(io.BytesIO):
        writes = 0

        def write(self, b):
            Closing.writes += 1
            if b == b": ka\n\n" and Closing.writes > 3:
                raise ConnectionResetError("client gone")
            return super().write(b)

    before = _slots(H)
    h = H.__new__(H)
    h.wfile = Closing()
    h.headers = {}
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h._stream_log_sse(run_id)  # must return once the keepalive write fails
    assert b": ka" in h.wfile.getvalue()
    assert _slots(H) == before
    db.close()
