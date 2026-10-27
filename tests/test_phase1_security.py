"""Phase 1 hardening: loopback auth bypass, CORS and control-file paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue.config import control_path, controls_dir
from taskqueue.web.handler import _loopback_bypass_ok


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "127.0.0.1"])
def test_loopback_bypass_allows_direct_local_hosts(host: str) -> None:
    assert _loopback_bypass_ok({"Host": host}) is True


@pytest.mark.parametrize("host", ["evil.example.com:8765", "rebind.attacker.net", ""])
def test_loopback_bypass_rejects_foreign_host_names(host: str) -> None:
    assert _loopback_bypass_ok({"Host": host}) is False


def test_loopback_bypass_rejects_proxied_requests() -> None:
    assert _loopback_bypass_ok({"Host": "127.0.0.1:8765", "X-Forwarded-For": "8.8.8.8"}) is False


def test_no_cors_header_on_api_responses(tmp_path: Path) -> None:
    """A cross-origin page must not be able to read the API: no Access-Control-Allow-Origin, ever."""
    import http.client
    import socket
    import time as _time

    from taskqueue.db import open_db
    from taskqueue.web import start_web_server

    with socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        port = _s.getsockname()[1]
    start_web_server(open_db(tmp_path / "q.db"), {}, {}, lambda: {}, db_path=tmp_path / "q.db",
                     port=port, bind_host="127.0.0.1")
    deadline = _time.monotonic() + 10
    for path in ("/api/status", "/api/dashboard", "/api/nope"):
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", path, headers={"Origin": "https://evil.example"})
                r = c.getresponse()
                r.read()
                break
            except OSError:
                if _time.monotonic() > deadline:
                    raise
                _time.sleep(0.1)
        assert r.getheader("Access-Control-Allow-Origin") is None, path


@pytest.mark.parametrize("task_id", ["../../evil", "a/b", "..\\..\\x", "C:evil"])
def test_control_path_stays_inside_controls_dir(tmp_path: Path, task_id: str) -> None:
    p = control_path(tmp_path, "proj", task_id)
    assert p.parent == controls_dir(tmp_path)
    assert ".." not in p.name and "/" not in p.name and "\\" not in p.name


def test_control_path_unchanged_for_normal_ids(tmp_path: Path) -> None:
    assert control_path(tmp_path, "proj", "task_1").name == "proj__task_1.json"
