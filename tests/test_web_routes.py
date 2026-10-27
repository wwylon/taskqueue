"""Route-level coverage for web.py — safety net for the dispatch table refactor.

Uses the same fake-handler pattern as test_security.py to exercise handler
methods without starting a real HTTP server.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from http.client import parse_headers
from pathlib import Path

import pytest

from taskqueue.db import get_config, open_db, parse_task_yaml
from taskqueue._db_tasks import get_task, upsert_task
from taskqueue.web import make_handler


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

def _make_handler(tmp_path: Path, projects=None):
    db = open_db(tmp_path / "q.db")
    return make_handler(
        db, {}, {}, lambda: (projects or {}),
        tmp_path / "q.db", "127.0.0.1"
    ), db


class _FakeWfile:
    def __init__(self):
        self.data = b""

    def write(self, d):
        self.data += d

    def flush(self):
        pass


def _make_request(HandlerClass, method: str, path: str, body: bytes = b"",
                  extra_headers: dict | None = None):
    """Instantiate a Handler, fire handle_one_request, return (status_code, body_bytes)."""
    headers_raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:8765\r\n"
        f"Content-Length: {len(body)}\r\n"
    )
    for k, v in (extra_headers or {}).items():
        headers_raw += f"{k}: {v}\r\n"
    headers_raw += "\r\n"
    raw = headers_raw.encode() + body

    wfile = _FakeWfile()
    status = []

    class TestHandler(HandlerClass):
        def setup(self):
            self.rfile = io.BufferedReader(io.BytesIO(raw))
            self.wfile = wfile
            self.raw_requestline = f"{method} {path} HTTP/1.1\r\n".encode()
            self.requestline = self.raw_requestline.decode()
            self.request_version = "HTTP/1.1"
            self.command = method
            self.path = path
            self.headers = parse_headers(io.BytesIO(headers_raw.encode().split(b"\r\n", 1)[1]))
            self.connection = "close"
            self.close_connection = True
            self.timeout = 5

        def send_response(self, code, msg=None):
            status.append(code)

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

        def log_message(self, *a):
            pass

    h = TestHandler.__new__(TestHandler)
    TestHandler.setup(h)
    h.handle_one_request()
    return status[0] if status else None, wfile.data


def _seed_task(db, project="proj", task_id="T1", status="queued"):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES (?, ?, ?, 'true', ?, ?, 1)",
        (project, task_id, status, now, now),
    )
    db.commit()


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

def test_get_status_200(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/status")
    assert code == 200
    data = json.loads(body)
    assert "queued" in data or "running_tasks" in data or isinstance(data, dict)


def test_get_status_counts_respect_project(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, project="proj", task_id="T1", status="failed")
    _seed_task(db, project="proj", task_id="T2", status="completed")
    _seed_task(db, project="other", task_id="T3", status="failed")
    code, body = _make_request(HandlerClass, "GET", "/api/status?project=proj")
    assert code == 200
    counts = json.loads(body)["status_counts"]
    assert counts.get("failed") == 1
    assert counts.get("completed") == 1
    assert "T3" not in json.dumps(counts)


def test_get_status_running_and_queued_respect_project(tmp_path: Path):
    """?project= must scope running[] and queued_count (not only status_counts)."""
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, project="proj", task_id="run_p", status="running")
    _seed_task(db, project="other", task_id="run_o", status="running")
    _seed_task(db, project="proj", task_id="q_p", status="queued")
    _seed_task(db, project="other", task_id="q_o", status="queued")
    code, body = _make_request(HandlerClass, "GET", "/api/status?project=proj")
    assert code == 200
    data = json.loads(body)
    run_ids = {t["task_id"] for t in data["running"]}
    assert run_ids == {"run_p"}
    assert data["queued_count"] == 1
    assert data["status_counts"].get("running") == 1
    assert data["status_counts"].get("queued") == 1

    code2, body2 = _make_request(HandlerClass, "GET", "/api/status")
    assert code2 == 200
    all_data = json.loads(body2)
    assert {t["task_id"] for t in all_data["running"]} == {"run_p", "run_o"}
    assert all_data["queued_count"] == 2


# ---------------------------------------------------------------------------
# GET /api/queue
# ---------------------------------------------------------------------------

def test_get_queue_empty(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/queue")
    assert code == 200
    data = json.loads(body)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    assert tasks == []


def test_get_queue_returns_task(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db)
    code, body = _make_request(HandlerClass, "GET", "/api/queue")
    assert code == 200
    data = json.loads(body)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    assert any(t["task_id"] == "T1" for t in tasks)


def test_get_queue_filters_by_project(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, project="proj")
    _seed_task(db, project="other", task_id="T2")
    code, body = _make_request(HandlerClass, "GET", "/api/queue?project=proj")
    assert code == 200
    data = json.loads(body)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    assert all(t["project"] == "proj" for t in tasks)


def test_get_queue_filters_by_status(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, status="queued")
    _seed_task(db, task_id="T2", status="completed")
    code, body = _make_request(HandlerClass, "GET", "/api/queue?status=completed")
    assert code == 200
    data = json.loads(body)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    assert all(t["status"] == "completed" for t in tasks)


def test_get_queue_search(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, task_id="alpha-task")
    _seed_task(db, task_id="beta-task")
    code, body = _make_request(HandlerClass, "GET", "/api/queue?search=alpha")
    assert code == 200
    data = json.loads(body)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    assert all("alpha" in t["task_id"] for t in tasks)


def test_get_locks_and_mutex_queue_fields(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, resources, last_error_code, block_reason) "
        "VALUES ('proj', 'holder', 'running', 'true', ?, ?, 1, ?, NULL, NULL)",
        (now, now, json.dumps({"locks": ["corpus"], "cpu_cores": 1, "ram_gib": 0.5})),
    )
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, resources, last_error_code, block_reason) "
        "VALUES ('proj', 'waiter', 'queued', 'true', ?, ?, 1, ?, 'tq.block.mutex', "
        "'lock(s) held by another running task: corpus')",
        (now, now, json.dumps({"locks": ["corpus"], "cpu_cores": 1, "ram_gib": 0.5})),
    )
    from taskqueue.db import set_project_limit
    set_project_limit(db, "proj", "max_tasks", 3.0)
    db.commit()

    code, body = _make_request(HandlerClass, "GET", "/api/locks")
    assert code == 200
    locks = json.loads(body)
    assert locks["occupancy"]["corpus"]["holder"] == "proj/holder"
    assert "proj/waiter" in locks["occupancy"]["corpus"]["waiters"]
    assert locks["contention"][0]["below_project_limit"] is True
    assert locks["contention"][0]["max_tasks"] == 3

    code, body = _make_request(HandlerClass, "GET", "/api/queue")
    assert code == 200
    tasks = json.loads(body)["tasks"]
    waiter = next(t for t in tasks if t["task_id"] == "waiter")
    assert waiter["lock_waiting_on"][0]["lock"] == "corpus"
    assert waiter["lock_waiting_on"][0]["holder"] == "proj/holder"
    assert "corpus" in (waiter.get("resources") or {}).get("locks", [])

    code, body = _make_request(HandlerClass, "GET", "/api/dashboard?tab=queue")
    assert code == 200
    dash = json.loads(body)
    assert isinstance(dash.get("etag"), str) and len(dash["etag"]) == 16
    assert dash["lock_contention"][0]["lock"] == "corpus"
    slim_waiter = next(t for t in dash["queue"]["tasks"] if t["task_id"] == "waiter")
    assert slim_waiter["lock_waiting_on"][0]["holder"] == "proj/holder"
    assert "locks" in slim_waiter["resources"]


# ---------------------------------------------------------------------------
# GET /api/history
# ---------------------------------------------------------------------------

def test_get_history_empty(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/history")
    assert code == 200
    data = json.loads(body)
    assert isinstance(data, list) or "runs" in data


def test_get_history_has_run(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, status="completed")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, log_path) "
        "VALUES ('proj', 'T1', ?, ?, 'completed', 'x.log')",
        (now, now),
    )
    db.commit()
    code, body = _make_request(HandlerClass, "GET", "/api/history")
    assert code == 200
    data = json.loads(body)
    runs = data if isinstance(data, list) else data.get("runs", [])
    assert any(r.get("task_id") == "T1" for r in runs)


def test_get_history_search(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    now = datetime.now(UTC).isoformat()
    db.executemany(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, log_path) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("proj", "alpha_task", now, now, "completed", "a.log"),
            ("proj", "beta_task", now, now, "completed", "b.log"),
        ],
    )
    db.commit()
    code, body = _make_request(HandlerClass, "GET", "/api/history?search=alpha")
    assert code == 200
    data = json.loads(body)
    runs = data if isinstance(data, list) else data.get("runs", [])
    assert len(runs) == 1
    assert runs[0]["task_id"] == "alpha_task"
    assert data.get("total") == 1


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------

def test_get_config_200(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/config")
    assert code == 200
    data = json.loads(body)
    assert isinstance(data, dict)


def test_post_config_accepts_web_save_cfg_payload(tmp_path: Path):
    """The config modal POSTs limit_decrease_policy with the numeric globals.

    Rejecting that one key used to 400 the whole body, so project limits
    saved (separate endpoint) while CPU/RAM limits silently did not.
    """
    HandlerClass, db = _make_handler(tmp_path)
    payload = {
        "cpu_limit_pct": 95,
        "gpu_limit_pct": 100,
        "ram_limit_pct": 80,
        "graceful_stop_sec": 30,
        "frozen_threshold_sec": 600,
        "max_running_tasks": 0,
        "limit_decrease_policy": "ignore",
        "max_cpu_cores": 0,
        "max_ram_gib": 0,
        "gpu_vram_limit_gib": 0,
    }
    code, body = _make_request(
        HandlerClass, "POST", "/api/config", body=json.dumps(payload).encode()
    )
    assert code == 200, body
    from taskqueue.db import open_db as _open
    cfg = get_config(_open(tmp_path / "q.db"))
    assert cfg["cpu_limit_pct"] == 95
    assert cfg["limit_decrease_policy"] == "ignore"


def test_post_force_marks_queued_task(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, status="queued")
    code, body = _make_request(HandlerClass, "POST", "/api/force/proj/T1")
    assert code == 200
    data = json.loads(body)
    assert data.get("ok") is True
    from taskqueue.db import open_db as _open
    row = get_task(_open(tmp_path / "q.db"), "proj", "T1")
    assert row["force_run"] == 1
    assert row["status"] == "queued"


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------

def test_get_projects_empty(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/projects")
    assert code == 200
    data = json.loads(body)
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# POST /api/stop — always 200 (endpoint does not 404 on missing task)
# ---------------------------------------------------------------------------

def test_post_stop_missing_task_404(tmp_path: Path):
    # 8.1 #13: stopping a nonexistent task is a 404, not a silent ok
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "POST", "/api/stop/proj/T_MISSING")
    assert code == 404


def test_post_stop_queued_task(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, status="queued")
    code, body = _make_request(HandlerClass, "POST", "/api/stop/proj/T1")
    assert code == 200
    data = json.loads(body)
    assert data.get("ok") is True


# ---------------------------------------------------------------------------
# POST /api/retry
# ---------------------------------------------------------------------------

def test_post_retry_missing_task_returns_false(tmp_path: Path):
    # retry returns {"ok": false} when task not found, not a 4xx
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "POST", "/api/retry/proj/T_MISSING")
    assert code == 200
    data = json.loads(body)
    assert data.get("ok") is False


def test_post_retry_failed_task(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db, status="failed")
    code, body = _make_request(HandlerClass, "POST", "/api/retry/proj/T1")
    assert code == 200
    data = json.loads(body)
    assert data.get("ok") is True
    # Verify via same connection (handler uses its own rconn, so open fresh)
    from taskqueue.db import open_db as _open
    db2 = _open(tmp_path / "q.db")
    row = db2.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "queued"


# ---------------------------------------------------------------------------
# POST /api/enable — toggles; body is not used by this endpoint
# ---------------------------------------------------------------------------

def test_post_enable_task(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    _seed_task(db)
    code, body = _make_request(HandlerClass, "POST", "/api/enable/proj/T1")
    assert code == 200
    data = json.loads(body)
    assert "ok" in data


def test_post_enable_missing_task_404(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, _ = _make_request(HandlerClass, "POST", "/api/enable/proj/NOPE")
    assert code == 404


# ---------------------------------------------------------------------------
# Unknown routes → 404
# ---------------------------------------------------------------------------

def test_unknown_get_route_404(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/nonexistent_route_xyz")
    assert code == 404


# ---------------------------------------------------------------------------
# GET /api/hardware
# ---------------------------------------------------------------------------

def test_get_hardware_200(tmp_path: Path):
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/hardware")
    assert code == 200
    data = json.loads(body)
    assert "ram" in data or "gpus" in data or isinstance(data, dict)


# Deep-link wiring (UI_TABS, _readUiUrl/_syncUrl/_applyUiVars, popstate, docs section) is behavior-tested in
# tests/test_js_app.py::test_ui_deep_link_boot_popstate_and_sync.


def test_dashboard_conditional_get_returns_304_without_changes(tmp_path: Path):
    from taskqueue.web import change_tracker
    from taskqueue.web import routes_read as rr

    change_tracker._TRACKERS.clear()
    rr._dash_cache.clear()
    rr._dash_inflight.clear()
    HandlerClass, db = _make_handler(tmp_path)
    code, body = _make_request(HandlerClass, "GET", "/api/dashboard?tab=queue")
    assert code == 200
    etag = json.loads(body)["etag"]
    hdr = {"If-None-Match": f'"{etag}"'}
    code, body = _make_request(HandlerClass, "GET", "/api/dashboard?tab=queue", extra_headers=hdr)
    assert code == 304 and body == b""
    # a commit from another connection changes the validator
    change_tracker._TRACKERS[str(tmp_path / "q.db")]._min_interval = 0
    _seed_task(db)
    code, _ = _make_request(HandlerClass, "GET", "/api/dashboard?tab=queue", extra_headers=hdr)
    assert code == 200


def test_dashboard_coalesces_concurrent_builds(tmp_path: Path):
    """Under load, identical dashboard GETs share one payload build (single-flight + short TTL)."""
    import http.client
    import socket
    import threading
    import time as _time

    from taskqueue.db import open_db
    from taskqueue.web import routes_read as rr
    from taskqueue.web import start_web_server

    rr._dash_cache.clear()
    rr._dash_inflight.clear()
    builds: list[float] = []
    orig = rr.ReadRoutes._build_dashboard_payload

    def _slow_build(self, *a, **k):
        builds.append(_time.monotonic())
        _time.sleep(0.2)
        return orig(self, *a, **k)

    rr.ReadRoutes._build_dashboard_payload = _slow_build  # type: ignore[method-assign]
    try:
        with socket.socket() as _s:
            _s.bind(("127.0.0.1", 0))
            port = _s.getsockname()[1]
        db = open_db(tmp_path / "q.db")
        start_web_server(
            db, {}, {}, lambda: {}, db_path=tmp_path / "q.db", port=port, bind_host="127.0.0.1",
        )
        deadline = _time.monotonic() + 10
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/api/dashboard?tab=queue")
                c.getresponse().read()
                c.close()
                break
            except OSError:
                if _time.monotonic() > deadline:
                    raise
                _time.sleep(0.05)
        rr._dash_cache.clear()
        rr._dash_inflight.clear()
        builds.clear()

        n = 8
        barrier = threading.Barrier(n)
        codes: list[int] = []
        lock = threading.Lock()

        def _one():
            barrier.wait()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", "/api/dashboard?tab=queue")
            r = c.getresponse()
            r.read()
            with lock:
                codes.append(r.status)
            c.close()

        threads = [threading.Thread(target=_one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert codes == [200] * n
        # One leader build; waiters reuse the cached body (TTL / single-flight).
        assert len(builds) == 1, builds
    finally:
        rr.ReadRoutes._build_dashboard_payload = orig  # type: ignore[method-assign]
        rr._dash_cache.clear()
        rr._dash_inflight.clear()


def test_index_sends_csp_and_hardening_headers(tmp_path):
    """The UI page carries a CSP without inline scripts; every response gets nosniff/frame headers."""
    import http.client
    import socket
    import time as _time

    from taskqueue.db import open_db
    from taskqueue.web import start_web_server

    with socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        port = _s.getsockname()[1]
    db = open_db(tmp_path / "q.db")
    start_web_server(db, {}, {}, lambda: {}, db_path=tmp_path / "q.db", port=port, bind_host="127.0.0.1")
    deadline = _time.monotonic() + 10
    while True:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/")
            r = c.getresponse()
            break
        except OSError:
            if _time.monotonic() > deadline:
                raise
            _time.sleep(0.1)
    body = r.read().decode("utf-8")
    csp = r.getheader("Content-Security-Policy") or ""
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("X-Frame-Options") == "DENY"
    assert "<script>" not in body  # no inline scripts: the CSP would block them
    c.request("GET", "/static/theme-boot.js")
    r2 = c.getresponse()
    assert r2.status == 200 and b"data-theme" in r2.read()
    etag = r2.getheader("ETag")
    assert etag and r2.getheader("Cache-Control") == "no-cache"
    c.request("GET", "/static/theme-boot.js", headers={"If-None-Match": etag})
    r3 = c.getresponse()
    assert r3.status == 304 and r3.read() == b""  # revalidated, not re-downloaded


def test_api_errors_are_json_not_html():
    """send_error on an /api/ path yields a JSON body (stdlib would send an HTML page)."""
    import io
    import json as _json

    from taskqueue.web import handler as hmod

    h = hmod.Handler.__new__(hmod.Handler)
    h.path = "/api/nope"
    h.command = "GET"
    h.request_version = "HTTP/1.1"
    h.requestline = "GET /api/nope HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    h.log_request = lambda *a, **k: None
    h.send_error(404, "no such route")
    raw = h.wfile.getvalue().decode()
    head, body = raw.split("\r\n\r\n", 1)
    assert "application/json" in head and "nosniff" in head
    assert _json.loads(body) == {"ok": False, "error": "no such route", "status": 404}


def test_project_stop_all_stops_only_that_projects_running_tasks(tmp_path):
    from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task

    H, db, _ = make_handler_env(tmp_path)
    for proj, tid in (("a", "t1"), ("a", "t2"), ("b", "t3")):
        seed_queued_task(db, project=proj, task_id=tid, dispatch="local")
    db.execute("UPDATE tasks SET status='running'")
    db.commit()
    status, body = http_request(H, "POST", "/api/project/stop-all/a")
    assert status == 200 and body == {"ok": True, "stopped": 2}
    flags = {r["task_id"]: r["stop_requested"] for r in db.execute("SELECT task_id, stop_requested FROM tasks")}
    assert flags == {"t1": 1, "t2": 1, "t3": 0}
    audit = db.execute("SELECT action, project FROM operator_audit WHERE action='stop_all'").fetchall()
    assert [(r["action"], r["project"]) for r in audit] == [("stop_all", "a")]
