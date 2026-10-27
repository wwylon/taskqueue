"""Offline PWA shell: manifest, service worker, and that /api is never cached."""

from __future__ import annotations

import json

from tests.helpers.master_worker import http_request, make_handler_env


def test_manifest_and_service_worker_served(tmp_path):
    handler, _db, _home = make_handler_env(tmp_path)
    status, body = http_request(handler, "GET", "/manifest.webmanifest")
    m = body if isinstance(body, dict) else json.loads(body)
    assert status == 200 and m["start_url"] == "/"
    status, sw = http_request(handler, "GET", "/sw.js")
    assert status == 200
    text = sw.decode() if isinstance(sw, bytes) else str(sw)
    assert "__TQ_VERSION__" not in text and "tq-shell-" in text


def test_service_worker_skips_api_and_index_links_manifest():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "taskqueue" / "static"
    sw = (static / "sw.js").read_text(encoding="utf-8")
    # allow-list: only the shell is cached, never /api/, /metrics, /docs, ...
    assert "startsWith('/static/')" in sw and "startsWith('/api/')" not in sw
    assert 'rel="manifest"' in (static / "index.html").read_text(encoding="utf-8")


def test_js_modules_served_and_in_offline_shell(tmp_path):
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "taskqueue" / "static"
    sw = (static / "sw.js").read_text(encoding="utf-8")
    modules = sorted(p.relative_to(static).as_posix() for p in (static / "js").rglob("*.js"))
    assert modules
    handler, _db, _home = make_handler_env(tmp_path)
    for rel in modules:
        assert f"'/static/{rel}'" in sw, f"{rel} missing from sw.js SHELL"
        status, body = http_request(handler, "GET", f"/static/{rel}")
        assert status == 200 and body, rel
    for bad in ("/static/js/../app.py", "/static/js/nope.js", "/static/js/%2e%2e/app.js", "/static/js/a.txt"):
        assert http_request(handler, "GET", bad)[0] == 404, bad


def test_index_loads_module_entry_before_app_js():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "taskqueue" / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index('type="module" src="/static/js/main.js"') < html.index('defer src="/static/app.js"')
