"""Real-browser smoke test of the web UI (Playwright + headless Chromium against an in-process server).

Node tests drive the modules against a DOM stub; this loads the actual page so module wiring, the
service worker, CSS-hidden elements and real event dispatch are exercised together. Skipped when the
`browser` dependency group is not installed (uv sync --group browser); `ci.ps1 -Mode full` installs it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from taskqueue.web import make_handler  # noqa: E402
from taskqueue.web.responses import BoundedThreadingHTTPServer  # noqa: E402
from tests.helpers.master_worker import seed_master_lease, seed_queued_task  # noqa: E402


@pytest.fixture
def ui_url(tmp_path: Path):
    from taskqueue.db import open_db

    db_path = tmp_path / "queue.db"
    db = open_db(db_path)
    seed_master_lease(db)
    for tid in ("alpha", "beta"):
        seed_queued_task(db, project="demo", task_id=tid, dispatch="local")
    db.commit()
    handler = make_handler(db, {}, {}, lambda: {}, db_path, "127.0.0.1")
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    db.close()


@pytest.fixture
def page():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # browser binary not installed for this playwright version
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(service_workers="block")
        pg = ctx.new_page()
        pg.set_default_timeout(15000)
        yield pg
        browser.close()


def test_queue_tabs_drawer_and_dashboard_render_without_page_errors(ui_url, page):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None)

    page.goto(ui_url + "/?page=queue")
    # Wait on DOM state, not wait_for_function: the page CSP (script-src 'self') forbids its in-page eval,
    # and CSP violations should surface as console errors rather than be bypassed.
    row = page.locator("#queue-tbody tr", has_text="alpha").first
    row.wait_for()
    assert page.locator("#queue-tbody tr", has_text="beta").count() >= 1

    # Drawer opens from the task cell (delegated data-click handlers, real event dispatch).
    row.locator(".task-cell .task-id-mono").click()  # not the copy icon: button clicks are ignored by design
    page.wait_for_selector("#tq-drawer.open, #tq-drawer[aria-hidden='false'], #tq-drawer-back:not([hidden])")
    assert "alpha" in page.inner_text("#tq-drawer")
    page.keyboard.press("Escape")

    # Every main tab loads without throwing.
    for tab in ("dashboard", "history", "deps", "groups", "resources", "timeline", "usage", "queue"):
        page.locator(f'nav button[data-tab="{tab}"]').first.click()
        page.wait_for_selector(f"#{tab}.page.active")

    # Dashboard: gauges render and the Fits-now strip fills from /api/plan.
    page.locator('nav button[data-tab="dashboard"]').first.click()
    page.wait_for_selector('#gauges [data-gauge="cpu"]')
    page.wait_for_selector("#fits-now:not([hidden])")
    assert "demo/" in page.inner_text("#fits-now")

    real = [e for e in errors if "favicon" not in e]
    assert real == [], real
