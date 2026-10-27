"""Handler mixin (extracted from make_handler)."""
from __future__ import annotations

# Broad shared namespace so method bodies keep working with minimal churn.
from .deps import (
    Path,
    _live_run_dict,
    _slim_status_payload,
    json,
)
from typing import Any
import threading
import time as _time_mod

# T-201 / F-146: one status compute per DB path shared across SSE clients (≤2s TTL).
_status_snap_lock = threading.Lock()
_status_snap_by_db: dict[str, dict[str, Any]] = {}
_STATUS_SNAP_TTL_SEC = 2.0


def clear_status_snapshot_cache() -> None:
    """Test helper."""
    with _status_snap_lock:
        _status_snap_by_db.clear()


SSE_KEEPALIVE_SEC = 15.0


class SSEMixin:
    def _shared_status(self) -> dict[str, Any]:
        """Return cached ``_get_status()`` for SSE fan-out (T-201)."""
        now = _time_mod.monotonic()
        db_key = str(getattr(self, "_db_path", "") or "")
        with _status_snap_lock:
            entry = _status_snap_by_db.get(db_key)
            if entry is not None and (now - float(entry["ts"])) < _STATUS_SNAP_TTL_SEC:
                return entry["data"]
        fresh = self._get_status()
        with _status_snap_lock:
            _status_snap_by_db[db_key] = {"ts": _time_mod.monotonic(), "data": fresh}
        return fresh

    def _stream_log_sse(self, run_id: int) -> None:
        """E1: GET /api/logs/<run_id>/stream — SSE stream of log output for a run."""
        # W1.3: share the SSE connection cap with /api/events. The slot is released by
        # this wrapper on every exit path (an error before the stream loop used to leak it).
        if not self._sse_semaphore.acquire(blocking=False):
            self._send_error_json(503, "Too many SSE connections; try again later")
            return
        try:
            self._stream_log_sse_body(run_id)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self._sse_semaphore.release()

    def _stream_log_sse_body(self, run_id: int) -> None:
        import time as _time
        from taskqueue.db import get_run

        row = get_run(self._rconn(), run_id)
        if not row or not row["log_path"]:
            # One retry after clearing a leftover snapshot (see _clear_stale_txn).
            self._clear_stale_txn()
            row = get_run(self._rconn(), run_id)
        if not row or not row["log_path"]:
            self.send_error(404)
            return
        log_path = Path(row["log_path"])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def _send(text: str, event_id: int | None = None) -> bool:
            # Cap individual SSE data lines to 4 KiB — very long lines
            # (e.g. 256 KB) cause EventSource parsers to buffer enormous
            # frames; wrapping keeps each data: line browser-friendly.
            # Lines split on newline only (the CR of CRLF is dropped): splitlines() would
            # also break on form feeds and other separators that are not line ends in a log.
            _SSE_LINE_CAP = 4096
            if text.endswith("\n"):
                text = text[:-1]  # a final newline ends the last line; it is not an extra empty line
            try:
                if event_id is not None:
                    # The byte offset lets a reconnecting EventSource resume via Last-Event-ID.
                    self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
                for line in (ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")):
                    if len(line) <= _SSE_LINE_CAP:
                        self.wfile.write(f"data: {line}\n".encode("utf-8"))
                    else:
                        for i in range(0, len(line), _SSE_LINE_CAP):
                            self.wfile.write(f"data: {line[i:i+_SSE_LINE_CAP]}\n".encode("utf-8"))
                self.wfile.write(b"\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        try:
            self.wfile.write(b"retry: 3000\n\n")  # reconnect delay hint for EventSource
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        try:
            resume_at = int(self.headers.get("Last-Event-ID") or -1)
        except (TypeError, ValueError):
            resume_at = -1

        # Send last 32 KiB of existing content (a reconnect with Last-Event-ID resumes instead)
        if resume_at < 0 and log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    if size > 32768:
                        f.seek(size - 32768)
                        f.readline()  # skip partial line
                    else:
                        f.seek(0)
                    init = f.read()
                if init and not _send(init):
                    return
            except OSError:
                pass

        offset = log_path.stat().st_size if log_path.exists() else 0
        if 0 <= resume_at <= offset:
            offset = resume_at
        import codecs

        # Multi-byte UTF-8 characters can straddle reads: decode incrementally and only
        # send complete lines; the tail is held until its newline arrives or the run ends.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        no_new_bytes_since: float | None = None
        poll_sleep = 0.5
        last_mtime: float | None = None
        last_write = _time.monotonic()

        try:
            while True:
                # Comment frame on a quiet log: surfaces a dead socket (write fails) so the slot is freed.
                if _time.monotonic() - last_write >= SSE_KEEPALIVE_SEC:
                    try:
                        self.wfile.write(b": ka\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    last_write = _time.monotonic()
                # F-041: also check DB for remote runs — self._active_runs is local only
                active_ids = {r.run_id for r in list(self._active_runs.values())}
                run_still_active = run_id in active_ids
                if not run_still_active:
                    db_row = self._rconn().execute(
                        "SELECT status FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                    run_still_active = bool(db_row and db_row["status"] == "running")
                if log_path.exists():
                    try:
                        st = log_path.stat()
                        size = st.st_size
                        mtime = st.st_mtime
                    except OSError:
                        size = 0
                        mtime = None
                    if size > offset:
                        try:
                            # F-023: use binary read so offset tracks bytes exactly
                            # (text mode on Windows converts CRLF→LF, desynchronising offset)
                            _cap = 256 * 1024
                            with open(log_path, "rb") as f:
                                f.seek(offset)
                                chunk_bytes = f.read(_cap)
                            offset += len(chunk_bytes)
                            pending += decoder.decode(chunk_bytes)
                            if offset > size:
                                offset = size
                            nl = pending.rfind("\n")
                            if nl != -1:
                                chunk, pending = pending[:nl], pending[nl + 1:]
                                # id = byte offset just past the last complete line sent
                                if not _send(chunk, offset - len(pending.encode("utf-8"))):
                                    return
                            last_write = _time.monotonic()
                            no_new_bytes_since = None
                            poll_sleep = 0.5
                            last_mtime = mtime
                        except OSError:
                            pass
                    elif not run_still_active:
                        if no_new_bytes_since is None:
                            no_new_bytes_since = _time.monotonic()
                        elif _time.monotonic() - no_new_bytes_since > 2.0:
                            pending += decoder.decode(b"", final=True)
                            if pending and not _send(pending, offset):
                                return
                            self._sse_log_end()
                            break
                    else:
                        # Quiet log: back off poll interval when mtime unchanged.
                        if mtime is not None and last_mtime is not None and mtime == last_mtime:
                            poll_sleep = min(2.0, poll_sleep * 1.5) if poll_sleep >= 0.5 else 2.0
                            if poll_sleep < 2.0:
                                poll_sleep = 2.0
                        else:
                            poll_sleep = 0.5
                        last_mtime = mtime
                elif not run_still_active:
                    self._sse_log_end()
                    break
                _time.sleep(poll_sleep)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sse_log_end(self) -> None:
        """Tell the log client the stream is complete so it does not treat the close as a drop."""
        try:
            self.wfile.write(b"event: end\ndata: end\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sse_event(self, event: str, data: Any) -> bool:
        """Write one named SSE event; return False if the client disconnected."""
        try:
            # The reconnect hint rides on the first event (one write per frame).
            prefix = getattr(self, "_sse_prefix", "")
            self._sse_prefix = ""
            payload = (
                f"{prefix}event: {event}\n"
                f"data: {json.dumps(data, default=str)}\n\n"
            ).encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _stream_sse(self) -> None:
        """Stream typed SSE: snapshot on connect, metrics every 2s, transition on start/stop."""
        if not self._sse_semaphore.acquire(blocking=False):
            self._send_error_json(503, "Too many SSE connections; try again later")
            return
        try:
            self._stream_sse_body()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self._sse_semaphore.release()

    def _stream_sse_body(self) -> None:
        import time
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self._sse_prefix = "retry: 5000\n\n"  # reconnect delay hint for EventSource
        prev_keys: set[tuple[str, str]] = set()
        prev_status: dict[tuple[str, str], str] = {}
        first = True
        try:
            while True:
                status = self._shared_status()
                running = status.get("running") or []
                cur_keys = {(t["project"], t["task_id"]) for t in running}
                cur_status = {
                    (t["project"], t["task_id"]): str(t.get("status") or "running")
                    for t in running
                }
                alive = True
                slim = _slim_status_payload(status)
                try:
                    from taskqueue.web.change_tracker import tracker_for

                    slim["data_version"] = tracker_for(self._db_path).token()
                except Exception:
                    pass

                if first:
                    alive = self._sse_event("snapshot", slim)
                    first = False
                else:
                    for t in running:
                        key = (t["project"], t["task_id"])
                        if key not in prev_keys:
                            # Wave C3: use prior known status when available; else "unknown".
                            from_st = prev_status.get(key) or "unknown"
                            alive = self._sse_event("transition", {
                                "project": t["project"],
                                "task_id": t["task_id"],
                                "from_status": from_st,
                                "to_status": "running",
                                "run_id": t.get("run_id"),
                            })
                            if not alive:
                                break
                    if alive:
                        for proj, tid in prev_keys - cur_keys:
                            alive = self._sse_event("transition", {
                                "project": proj,
                                "task_id": tid,
                                "from_status": "running",
                                "to_status": "terminal",
                            })
                            if not alive:
                                break
                    if alive:
                        alive = self._sse_event("metrics", {
                            "running": [_live_run_dict(t) for t in running],
                            "queued_count": status.get("queued_count", 0),
                        })

                # T-201: also emit default ``message`` so browser onmessage works.
                # Named events above carry transitions; keep one slim default payload.
                if alive:
                    try:
                        self.wfile.write(
                            f"data: {json.dumps(slim, default=str)}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        alive = False

                if not alive:
                    break
                prev_keys = cur_keys
                prev_status = {**prev_status, **cur_status}
                # Bound prev_status growth to recently seen keys.
                if len(prev_status) > 500:
                    keep = set(prev_keys)
                    prev_status = {k: v for k, v in prev_status.items() if k in keep}
                # Idle (nothing running): nothing live to animate, so poll less often.
                time.sleep(2.0 if running else 5.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
