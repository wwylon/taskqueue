"""Behavior tests for the /api/events stream (snapshot, disconnect cleanup, cap)."""
from __future__ import annotations

import json
from pathlib import Path

from taskqueue.web import sse
from tests.helpers.master_worker import http_request, make_handler_env


class _DyingWfile:
    """Accepts ``ok_writes`` writes, then behaves like a closed client socket."""

    def __init__(self, ok_writes: int) -> None:
        self.ok = ok_writes
        self.data = b""

    def write(self, d: bytes) -> None:
        if self.ok <= 0:
            raise BrokenPipeError
        self.ok -= 1
        self.data += d

    def flush(self) -> None:
        pass


def _handler(H, wfile, status):
    h = H.__new__(H)
    h.wfile = wfile
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h._get_status = lambda: status
    return h


def test_events_rejects_when_cap_exhausted(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    held = 0
    while H._sse_semaphore.acquire(blocking=False):
        held += 1
    try:
        status, _ = http_request(H, "GET", "/api/events")
        assert status == 503
    finally:
        for _ in range(held):
            H._sse_semaphore.release()
    db.close()


def test_events_sends_snapshot_and_releases_slot_on_disconnect(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    sse.clear_status_snapshot_cache()
    before = H._sse_semaphore._value
    wfile = _DyingWfile(ok_writes=1)  # snapshot goes out, the next write dies
    h = _handler(H, wfile, {"running": [], "queued_count": 3})
    try:
        h._stream_sse()  # must return (not loop forever) once the client is gone
    finally:
        sse.clear_status_snapshot_cache()
    text = wfile.data.decode()
    assert text.startswith("retry: 5000\n\nevent: snapshot\n")
    payload = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
    assert isinstance(payload, dict)
    assert H._sse_semaphore._value == before
    db.close()
