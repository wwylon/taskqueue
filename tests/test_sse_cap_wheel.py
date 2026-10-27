"""8.1 HIGH: SSE streams may not exhaust the HTTP pool; docs ship with the package; webhooks don't follow remote redirects."""
from __future__ import annotations

import http.server
import threading
from urllib.error import URLError

import pytest

from taskqueue import notify
from taskqueue.web import responses


def test_sse_cap_is_quarter_of_pool() -> None:
    assert responses.SSE_MAX_STREAMS * 4 <= responses._DEFAULT_HTTP_WORKERS
    assert responses.SSE_MAX_STREAMS >= 2


def test_docs_root_resolves_to_manifest() -> None:
    from taskqueue import docs_loader

    assert (docs_loader.docs_root() / "manifest.json").is_file()


def test_pyproject_force_includes_docs() -> None:
    import tomllib
    from pathlib import Path

    cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    fi = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert fi["docs"] == "taskqueue/_docs"


def test_webhook_redirect_to_remote_host_is_blocked_before_request() -> None:
    hits: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            hits.append(self.path)
            # Drain the body first: answering unread makes Windows abort the client socket (WinError 10053).
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(307)
            self.send_header("Location", "http://example.invalid/steal")
            self.end_headers()

        def log_message(self, *a) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(URLError):
            notify._attempt_post(f"http://127.0.0.1:{srv.server_address[1]}/hook", {"a": 1})
    finally:
        srv.shutdown()
    assert hits == ["/hook"]
