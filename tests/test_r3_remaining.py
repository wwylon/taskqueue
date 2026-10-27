"""Round 3 remaining — F-072 master-status UTC, F-073 stream + bounded HTTP."""

from __future__ import annotations

import io
import time
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.web import (
    BoundedThreadingHTTPServer,
    stream_request_body_to_file,
)


def test_master_status_labels_utc_correctly(tmp_path: Path, capsys) -> None:
    """F-072: expires_at is unix epoch — format with gmtime, not localtime."""
    from taskqueue._cli_admin import cmd_master_status

    db = open_db(tmp_path / "queue.db")
    expires = 1_700_000_000  # fixed epoch
    db.execute(
        "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at, epoch) "
        "VALUES (1, 'host:1:abc', ?, 1)",
        (expires,),
    )
    db.commit()
    db.close()

    cmd_master_status(tmp_path)
    out = capsys.readouterr().out
    expected = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(expires))
    assert f"{expected} UTC" in out
    local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires))
    if local != expected:
        assert f"{local} UTC" not in out


def test_stream_request_body_to_file(tmp_path: Path) -> None:
    """F-073: body is copied in chunks; dest equals payload."""
    payload = b"abcdefgh" * 10_000
    dest = tmp_path / "out" / "file.bin"
    stream_request_body_to_file(io.BytesIO(payload), len(payload), dest, chunk=1024)
    assert dest.read_bytes() == payload
    # no leftover partials
    assert list((tmp_path / "out").glob("*.partial")) == []


def test_stream_truncated_raises(tmp_path: Path) -> None:
    dest = tmp_path / "f.bin"
    try:
        stream_request_body_to_file(io.BytesIO(b"short"), 100, dest)
        assert False, "expected OSError"
    except OSError:
        pass
    assert not dest.exists()


def test_bounded_http_server_uses_pool() -> None:
    """F-073: server class is pool-backed, not unbounded ThreadingHTTPServer."""
    from http.server import BaseHTTPRequestHandler

    class _H(BaseHTTPRequestHandler):
        def handle(self) -> None:
            return

        def log_message(self, *a) -> None:
            return

    srv = BoundedThreadingHTTPServer(("127.0.0.1", 0), _H, max_workers=4)
    try:
        assert srv._pool._max_workers == 4
    finally:
        srv.server_close()


def test_bounded_http_server_drops_connections_past_inflight_cap() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler

    gate = threading.Event()

    class _H(BaseHTTPRequestHandler):
        def handle(self) -> None:
            gate.wait(5)

        def log_message(self, *a) -> None:
            return

    class _Sock:
        closed = False

        def close(self) -> None:
            self.closed = True

        def shutdown(self, *_a) -> None:
            pass

    srv = BoundedThreadingHTTPServer(("127.0.0.1", 0), _H, max_workers=1)
    try:
        socks = [_Sock() for _ in range(12)]
        for s in socks:
            srv.process_request(s, ("127.0.0.1", 1))
        # cap = 1 * 8 in flight; the other 4 are refused at once.
        assert sum(s.closed for s in socks) == 4
        gate.set()
    finally:
        gate.set()
        srv.server_close()


def test_longpoll_budget_degrades_to_short_poll_when_slots_exhausted() -> None:
    from taskqueue.web.handler import Handler
    from taskqueue.web.routes_worker import LONGPOLL_SLOTS

    taken = 0
    while LONGPOLL_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        h = Handler.__new__(Handler)
        assert h._longpoll_budget(20.0) == 0.0
        assert h._longpoll_budget(0.0) == 0.0
    finally:
        for _ in range(taken):
            LONGPOLL_SLOTS.release()
    h2 = Handler.__new__(Handler)
    assert h2._longpoll_budget(20.0) == 20.0 and h2._lp_held
    h2.finish = Handler.finish.__get__(h2)
    try:
        Handler.finish(h2)
    except Exception:
        pass  # no real socket files on this bare instance
    assert not h2._lp_held
