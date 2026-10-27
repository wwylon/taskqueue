"""MCP log paging must not skip data when a finished run's log is read in pages."""
from __future__ import annotations

import io
import json
from pathlib import Path

from taskqueue import config as tqconfig
from taskqueue import mcp_server
from taskqueue._db_runs import insert_run
from taskqueue.db import open_db


def test_finished_run_paging_does_not_skip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tqconfig, "db_path", lambda home: tmp_path / "queue.db")
    conn = open_db(tmp_path / "queue.db")
    log = tmp_path / "r.log"
    log.write_text("x" * 100_000, encoding="utf-8")
    rid = insert_run(conn, "p", "t", str(log))
    conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (rid,))
    conn.commit()
    conn.close()

    first = mcp_server._log_tail(tmp_path, {"project": "p", "task_id": "t", "offset": 0, "max_bytes": 4096})
    assert len(first["text"]) < 100_000
    assert first["next_offset"] == first["offset"] + len(first["text"].encode())
    assert first["stream_done"] is False

    big = mcp_server._log_tail(
        tmp_path, {"project": "p", "task_id": "t", "offset": first["next_offset"], "max_bytes": 10_000_000}
    )
    assert big["stream_done"] is True


def test_run_stdio_ignores_non_dict_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("[1,2]\n42\n" + json.dumps({"method": "tools/list", "id": 1}) + "\n"))
    sent = []
    monkeypatch.setattr(mcp_server, "_respond", lambda i, r: sent.append(i))
    assert mcp_server.run_stdio(tmp_path) == 0
    assert sent == [1]


def test_run_stdio_non_dict_params(tmp_path: Path, monkeypatch) -> None:
    line = json.dumps({"method": "tools/list", "id": 7, "params": [1]})
    monkeypatch.setattr("sys.stdin", io.StringIO(line + "\n"))
    sent = []
    monkeypatch.setattr(mcp_server, "_respond", lambda i, r: sent.append(i))
    assert mcp_server.run_stdio(tmp_path) == 0
    assert sent == [7]
