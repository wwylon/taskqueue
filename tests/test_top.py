"""tq top: SSE frame parsing, rendering, and one live frame from a real web server."""
from __future__ import annotations

import io
import socket
from pathlib import Path

from taskqueue.top import iter_sse, render, run_top


def test_iter_sse_parses_named_and_default_events():
    lines = ["retry: 5000", "", ": ka", "", "event: snapshot", 'data: {"running": []}', "",
             'data: {"queued_count": 2}', "", "data: not-json", ""]
    assert list(iter_sse(lines)) == [("snapshot", {"running": []}), ("message", {"queued_count": 2}), ("message", "not-json")]


def test_render_lists_running_tasks():
    frame = render({"queued_count": 3, "running": [
        {"project": "p", "task_id": "t", "progress_pct": 42.4, "elapsed_sec": 125, "current_ram_gib": 1.5,
         "current_cpu_pct": 90, "last_log_line": "epoch 3"}]})
    assert "queued 3" in frame and "p/t" in frame and "42%" in frame and "2m05s" in frame and "epoch 3" in frame


def test_run_top_once_against_live_server(tmp_path: Path, monkeypatch):
    from taskqueue.db import open_db, set_config
    from taskqueue.web import start_web_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db = open_db(tmp_path / "queue.db")
    set_config(db, "web_port", port)
    start_web_server(db, {}, {}, lambda: {}, db_path=tmp_path / "queue.db", port=port, bind_host="127.0.0.1")
    monkeypatch.setenv("TQ_MASTER", f"http://127.0.0.1:{port}")
    out = io.StringIO()
    assert run_top(tmp_path, once=True, out=out) == 0
    assert "tq top" in out.getvalue() and "(nothing running)" in out.getvalue()


def test_run_top_json_emits_one_ndjson_line(tmp_path: Path, monkeypatch):
    import json

    from taskqueue.db import open_db, set_config
    from taskqueue.web import start_web_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db = open_db(tmp_path / "queue.db")
    set_config(db, "web_port", port)
    start_web_server(db, {}, {}, lambda: {}, db_path=tmp_path / "queue.db", port=port, bind_host="127.0.0.1")
    monkeypatch.setenv("TQ_MASTER", f"http://127.0.0.1:{port}")
    out = io.StringIO()
    assert run_top(tmp_path, once=True, as_json=True, out=out) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 1 and "\x1b" not in lines[0]
    assert isinstance(json.loads(lines[0]), dict)
