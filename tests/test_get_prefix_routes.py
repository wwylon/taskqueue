"""Wave 4.5: the GET prefix route table keeps the old elif chain's first-match behaviour."""

from __future__ import annotations

import pytest

from tqdaemon.web.handler import _dispatch_get_prefix


class Rec:
    """Records which handler method a path reached, with its arguments."""

    _system_info: dict = {}

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_serve") or name in ("_stream_log_sse", "_proxy_service_request", "_get_task_detail"):
            return lambda *a: self.calls.append((name, *a)) or {}
        raise AttributeError(name)

    def send_error(self, code):
        self.calls.append(("send_error", code))

    def _send_error_json(self, code, msg):
        self.calls.append(("json_error", code, msg))

    def _json(self, data, status=200):
        self.calls.append(("json", status))


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/docs/scheduler", ("_serve_docs", "/docs/scheduler")),
        ("/static/locales/pl.json", ("_serve_locale", "/static/locales/pl.json")),
        ("/static/js/main.js", ("_serve_js_module", "/static/js/main.js")),
        ("/api/bundle/p/abc", ("_serve_bundle", "p", "abc")),
        ("/api/bundle/p/abc/x", ("send_error", 400)),
        ("/api/package/p/h", ("_serve_package_overlay", "p", "h")),
        ("/api/transfer/get/p/a/b.txt", ("_serve_transfer_file", "p", "a/b.txt")),
        ("/api/transfer/get/p", ("json_error", 400, "Missing path")),
        ("/api/data/blob/deadbeef", ("_serve_cas_blob", "deadbeef")),
        ("/api/data/snapshot/7/db/x.sqlite", ("_serve_sqlite_snapshot", "7", "db/x.sqlite")),
        ("/api/data/snapshot/7", ("json_error", 400, "Missing db path")),
        ("/api/sync/fingerprint/p", ("_serve_sync_fingerprint", "p")),
        ("/api/sync/manifest/p", ("_serve_sync_manifest", "p")),
        ("/api/sync/file/p/x/y", ("_serve_sync_file", "p", "x/y")),
        ("/api/data/manifest/12", ("_serve_data_manifest_for_run", "12")),
        ("/api/data/manifest/proj", ("_serve_data_manifest", "proj")),
        ("/api/data/file/p", ("send_error", 400)),
        ("/api/logs/42/stream", ("_stream_log_sse", 42)),
        ("/api/logs/x/stream", ("send_error", 400)),
        ("/api/logs/42", ("_serve_logs", "/api/logs/42")),
        ("/api/runs/9", ("_serve_run_detail", 9)),
        ("/api/runs/9/history", ("_serve_run_history", 9)),
        ("/api/tasks/p/t/yaml", ("_serve_task_yaml", "p", "t")),
        ("/api/tasks/p/a/b/runs", ("_serve_task_runs", "p", "a/b")),
        ("/api/tasks/p/runs", ("send_error", 400)),
        ("/api/tasks/p/t/log-search", ("_serve_log_search", "p", "t")),
        ("/api/service/ollama/api/ps", ("_proxy_service_request", "ollama", "api/ps")),
        ("/api/tasks/p/t", ("_get_task_detail", "p", "t")),
        ("/api/tasks/p", ("send_error", 400)),
    ],
)
def test_prefix_route(path, expected):
    h = Rec()
    assert _dispatch_get_prefix(h, path) is True
    assert h.calls[0] == expected


@pytest.mark.parametrize("path", ["/api/nope", "/api/runs/abc", "/static/other.css"])
def test_unmatched_is_404(path):
    assert _dispatch_get_prefix(Rec(), path) is False
