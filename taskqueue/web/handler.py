"""Module-level HTTP Handler composed of route mixins."""
from __future__ import annotations

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from taskqueue.sse_ticket import is_stream_path, redeem_ticket

from .context import HandlerContext
from .deps import (
    _API_REGISTRY,
    _STATIC,
    _get_auth_config,
    _get_hardware_data,
    _redact_config,
    build_capacity,
    enable_task,
    extract_request_token,
    force_task,
    get_all_project_limits,
    get_config,
    get_running_tasks,
    holds_master_lease,
    is_worker_path,
    json,
    parse_qs,
    pause_task,
    project_scope_allows,
    request_stop_with_control,
    resolve_access,
    resolve_token_projects,
    scope_allows,
    set_project_frozen,
    unquote,
    urlparse,
)
from .routes_read import ReadRoutes
from .routes_transfer import TransferRoutes
from .routes_worker import WorkerRoutes
from .routes_write import WriteRoutes
from .sse import SSEMixin

_PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP")


def _host_is_local_name(host_header: str) -> bool:
    """True when the Host header names this machine directly (not a rebound DNS name)."""
    import ipaddress
    import socket

    h = (host_header or "").strip().lower()
    if h.startswith("["):  # [::1]:8765
        h = h[1:].split("]")[0]
    elif h.count(":") == 1:
        h = h.split(":")[0]
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    try:
        return h in {socket.gethostname().lower(), socket.getfqdn().lower()}
    except OSError:
        return False


def _loopback_bypass_ok(headers: Any) -> bool:
    """Loopback clients skip token auth only for direct requests.

    A reverse proxy makes every client look like loopback, and a DNS-rebinding page
    reaches us with an attacker-chosen Host name; neither may use the bypass.
    """
    if any(headers.get(h) for h in _PROXY_HEADERS):
        return False
    return _host_is_local_name(headers.get("Host") or "")



# The UI is same-origin and has no inline scripts (theme-boot.js, data-click delegation).
# Inline styles remain in rendered markup, so style-src keeps 'unsafe-inline'.
_UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'self'; "
    "frame-ancestors 'none'; form-action 'self'"
)

class Handler(
    ReadRoutes,
    WriteRoutes,
    WorkerRoutes,
    TransferRoutes,
    SSEMixin,
    BaseHTTPRequestHandler,
):
    """HTTP handler. Bound subclasses set class attrs via ``make_handler``."""

    # Set by make_handler on BoundHandler
    _ctx: HandlerContext
    _db_path: Path
    _projects_ref: Callable[[], dict]
    _active_runs: dict
    _system_info: dict[str, Any]
    _bind_host: str
    _pending_sync: set[str]
    _pending_file_sync: set[str]
    _sse_semaphore: threading.Semaphore
    _tls: threading.local
    _conn: sqlite3.Connection  # original daemon conn (rarely used)
    _pending_commands: dict
    _commands_condition: threading.Condition

    # A client that stalls mid-request (or mid-read) must not pin a pool worker forever.
    timeout = 120

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if getattr(self, "_lp_held", False):
                self._lp_held = False
                from taskqueue.web.routes_worker import LONGPOLL_SLOTS

                LONGPOLL_SLOTS.release()

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def send_response(self, code: int, message: str | None = None) -> None:
        self._tq_status = code  # captured for usage counters
        super().send_response(code, message)

    def _rconn(self) -> sqlite3.Connection:
        """Per-thread read connection — avoids races with the daemon's write conn."""
        from taskqueue.db import open_db as _open_db
        tls = type(self)._tls
        if not getattr(tls, "conn", None):
            tls.conn = _open_db(type(self)._db_path, migrate=False)
        return tls.conn

    def _clear_stale_txn(self) -> None:
        """Drop abandoned txns on reused ThreadPool threads."""
        tls = type(self)._tls
        for attr in ("conn", "wconn"):
            conn = getattr(tls, attr, None)
            if conn is not None and conn.in_transaction:
                conn.rollback()

    def _open_write_conn(self, timeout: float = 10.0) -> sqlite3.Connection:
        """W5.4: short-lived write connection that applies the same pragmas as open_db."""
        from taskqueue.db import open_write_conn as _owc
        return _owc(type(self)._db_path, timeout)

    def _wconn(self) -> sqlite3.Connection:
        """Wave D4: per-thread write connection for mutating routes (not the read TLS conn)."""
        from taskqueue.db import open_write_conn as _owc
        tls = type(self)._tls
        wconn = getattr(tls, "wconn", None)
        if wconn is None:
            tls.wconn = _owc(type(self)._db_path, timeout=10.0)
        return tls.wconn

    def _get_all_projects(self) -> list[str]:
        """Sorted list of all known projects (registered + ever seen in DB), cached 10s."""
        import time as _time
        from . import responses as _resp

        now = _time.monotonic()
        db_key = str(getattr(self, "_db_path", "") or "")
        with _resp._projects_lock:
            entry = _resp._projects_cache_by_db.get(db_key)
            if entry is not None and now - entry[0] < _resp._PROJECTS_CACHE_TTL:
                return list(entry[1])
        db_projects = {
            r[0] for r in self._rconn().execute("SELECT DISTINCT project FROM tasks").fetchall()
        }
        fresh = sorted(set(self._projects_ref().keys()) | db_projects)
        with _resp._projects_lock:
            _resp._projects_cache_by_db[db_key] = (_time.monotonic(), fresh)
            # Keep legacy module attrs in sync for any external readers.
            _resp._projects_cache = fresh
            _resp._projects_cache_ts = _time.monotonic()
            return list(fresh)

    def _query(self) -> dict:
        """Parsed query string of the current request, cached per ``self.path``.

        Keyed by path because one handler object serves several keep-alive requests.
        Treat the result as read-only.
        """
        cached = self.__dict__.get("_query_cache")
        if cached is None or cached[0] != self.path:
            cached = (self.path, parse_qs(urlparse(self.path).query))
            self.__dict__["_query_cache"] = cached
        return cached[1]

    def _project_from_qs(self) -> str | None:
        qs = self._query()
        p = qs.get("project", [None])[0]
        return p

    def _check_auth(self) -> bool:
        """Return False if response already sent (401/403)."""

        path = urlparse(self.path).path.rstrip("/") or "/"
        if not path.startswith("/api"):
            return True
        cfg = _get_auth_config(self._rconn(), self._db_path)

        # W1.1 / F-064: CSRF — reject foreign Origin on state-changing methods.
        # GET /api/worker/next-task mutates queue state (claim), so it is included.
        # Browser same-origin requests typically omit Origin; cross-site navigations send it.
        needs_csrf = self.command in ("POST", "PUT", "PATCH", "DELETE") or path == "/api/worker/next-task"
        # Browsers label cross-site fetches; the UI is same-origin, so reject them for
        # reads too (otherwise any web page could read the API from a loopback daemon).
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            self._send_error_json(403, "Cross-site request rejected")
            return False
        if needs_csrf:
            origin = self.headers.get("Origin") or ""
            if origin:
                o = urlparse(origin)
                host_hdr = (self.headers.get("Host") or "").lower()
                expected_host = host_hdr.split(":")[0]
                origin_host = (o.hostname or "").lower()
                # F-054: host must match; also reject different ports (localhost CSRF).
                if origin_host != expected_host:
                    self._send_error_json(403, "Cross-origin request rejected")
                    return False
                try:
                    expected_port = int(host_hdr.split(":")[1]) if ":" in host_hdr else None
                except ValueError:
                    expected_port = None
                origin_port = o.port
                if (
                    expected_port is not None
                    and origin_port is not None
                    and origin_port != expected_port
                ):
                    self._send_error_json(403, "Cross-origin request rejected")
                    return False

        # Same-machine localhost bypass: skip token check for loopback clients.
        client_ip = getattr(self, "client_address", (None,))[0] or ""
        if client_ip in ("127.0.0.1", "::1", "localhost") and _loopback_bypass_ok(self.headers):
            return True

        qs = self._query()
        qtok = (qs.get("tq_token") or qs.get("token") or [None])[0]
        provided = extract_request_token(dict(self.headers), qtok)
        ticket = (qs.get("tq_ticket") or [None])[0]
        if ticket and not provided and self.command == "GET" and is_stream_path(path):
            provided = redeem_ticket(ticket)
        scope, via = resolve_access(cfg, self._rconn(), provided)
        if scope is None:
            body = b'{"error":"unauthorized","hint":"Authorization: Bearer <token> or X-TQ-Token"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(body)
            return False
        if via == "token" and is_worker_path(path):
            self._send_error_json(403, "workers and the master lease require auth_token")
            return False
        if not scope_allows(scope, self.command, path):
            self._send_error_json(403, "token scope does not allow this request")
            return False
        if via == "token":
            allowed = resolve_token_projects(self._rconn(), provided)
            if allowed is not None and not project_scope_allows(allowed, self.command, path, qs):
                self._send_error_json(403, "token is limited to projects; only project-filtered reads are allowed")
                return False
        return True

    def _require_master_lease(self, *, next_task: bool = False) -> bool:
        """F-057: refuse worker mutations unless this process holds the lease."""
        if holds_master_lease(self._rconn()):
            return True
        payload: dict[str, Any] = {"ok": False, "error": "not master"}
        if next_task:
            payload["task"] = None
        self._json(payload, 503)
        return False

    def _send_error_json(self, code: int, msg: str) -> None:
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._clear_stale_txn()
        if not self._check_auth():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"

        # Exact-match dispatch (O(1) lookup)
        route = _GET_EXACT.get(path)
        if route is not None:
            route(self)
            return

        # Prefix-match dispatch (ordered; first match wins)
        if path.startswith("/docs/"):
            self._serve_docs(path)
        elif path.startswith("/static/locales/"):
            self._serve_locale(path)
        elif path.startswith("/static/js/"):
            self._serve_js_module(path)
        elif path.startswith("/api/bundle/"):
            parts = path[len("/api/bundle/"):].split("/")
            if len(parts) == 2:
                self._serve_bundle(parts[0], parts[1])
            else:
                self.send_error(400)
        elif path.startswith("/api/package/"):
            parts = path[len("/api/package/"):].split("/")
            if len(parts) == 2:
                self._serve_package_overlay(parts[0], parts[1])
            else:
                self.send_error(400)
        elif path.startswith("/api/transfer/get/"):
            rest_t = path[len("/api/transfer/get/"):]
            proj_name_t, sep_t, rel_t = rest_t.partition("/")
            if sep_t:
                self._serve_transfer_file(proj_name_t, rel_t)
            else:
                self._send_error_json(400, "Missing path")
        elif path.startswith("/api/data/blob/"):
            # D1: CAS blob fetch — /api/data/blob/<sha256>
            sha256 = path[len("/api/data/blob/"):]
            self._serve_cas_blob(sha256)
        elif path.startswith("/api/data/snapshot/"):
            # D2: SQLite snapshot — /api/data/snapshot/<run_id>/<rel_path>
            rest_snap = path[len("/api/data/snapshot/"):]
            snap_run_id, snap_sep, snap_rel = rest_snap.partition("/")
            if snap_sep:
                self._serve_sqlite_snapshot(snap_run_id, snap_rel)
            else:
                self._send_error_json(400, "Missing db path")
        elif path.startswith("/api/sync/fingerprint/"):
            self._serve_sync_fingerprint(path[len("/api/sync/fingerprint/"):])
        elif path.startswith("/api/sync/manifest/"):
            self._serve_sync_manifest(path[len("/api/sync/manifest/"):])
        elif path.startswith("/api/sync/file/"):
            rest_s = path[len("/api/sync/file/"):]
            proj_s, sep_s, rel_s = rest_s.partition("/")
            if sep_s:
                self._serve_sync_file(proj_s, rel_s)
            else:
                self._send_error_json(400, "Missing path")
        elif path.startswith("/api/data/manifest/"):
            # D1 per-run manifest (numeric run_id) vs legacy project manifest
            suffix = path[len("/api/data/manifest/"):]
            if suffix.isdigit():
                self._serve_data_manifest_for_run(suffix)
            else:
                self._serve_data_manifest(suffix)
        elif path.startswith("/api/data/file/"):
            rest = path[len("/api/data/file/"):]
            proj_name, sep, rel_path = rest.partition("/")
            if sep:
                self._serve_data_file(proj_name, rel_path)
            else:
                self.send_error(400)
        elif path.startswith("/api/logs/"):
            rest = path[len("/api/logs/"):]
            if rest.endswith("/stream"):
                try:
                    self._stream_log_sse(int(rest[:-len("/stream")]))
                except ValueError:
                    self.send_error(400)
            else:
                self._serve_logs(path)
        elif path.startswith("/api/runs/") and path[len("/api/runs/"):].isdigit():
            self._serve_run_detail(int(path[len("/api/runs/"):]))
        elif path.startswith("/api/runs/") and path.endswith("/history"):
            try:
                self._serve_run_history(int(path[len("/api/runs/"):-len("/history")]))
            except ValueError:
                self.send_error(400)
        elif path.startswith("/api/tasks/") and path.endswith("/yaml"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/yaml")].partition("/")
            if sep:
                self._serve_task_yaml(proj, tid)
            else:
                self.send_error(400)
        elif path.startswith("/api/tasks/") and path.endswith("/log-search"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/log-search")].partition("/")
            if sep:
                self._serve_log_search(proj, tid)
            else:
                self.send_error(400)
        elif path.startswith("/api/tasks/") and path.endswith("/runs"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/runs")].partition("/")
            if sep:
                self._serve_task_runs(proj, tid)
            else:
                self.send_error(400)
        elif path.startswith("/api/tasks/") and path.endswith("/why"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/why")].partition("/")
            if not sep:
                self.send_error(400)
                return
            from urllib.parse import unquote

            from taskqueue.why import explain_task

            info = explain_task(self._rconn(), unquote(proj), unquote(tid), system=self._system_info or None)
            self._json(info, status=200 if info.get("ok") else 404)
        elif path.startswith("/api/service/"):
            # D3: HTTP-proxy service broker — /api/service/<name>/<subpath>
            rest_svc = path[len("/api/service/"):]
            svc_name, svc_sep, svc_sub = rest_svc.partition("/")
            if svc_name:
                self._proxy_service_request(svc_name, svc_sub)
            else:
                self._send_error_json(400, "Missing service name")
        elif path.startswith("/api/tasks/"):
            proj, sep, tid = path[len("/api/tasks/"):].partition("/")
            if sep and tid:
                self._json(self._get_task_detail(proj, tid))
            else:
                self.send_error(400)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        self._clear_stale_txn()
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Exact-match dispatch
        route = _POST_EXACT.get(path)
        if route is not None:
            route(self)
            return

        # Prefix-match dispatch
        if path.startswith("/api/groups/") and path.endswith("/stop"):
            self._bulk(lambda: self._group_stop(unquote(path[len("/api/groups/"):-len("/stop")]), qs))
        elif path.startswith("/api/groups/") and path.endswith("/retry"):
            self._bulk(lambda: self._group_retry(unquote(path[len("/api/groups/"):-len("/retry")]), qs))
        elif path.startswith("/api/suites/") and path.endswith("/retry"):
            self._bulk(lambda: self._suite_retry(unquote(path[len("/api/suites/"):-len("/retry")]), qs))
        elif path.startswith("/api/notify/dead/") and path.endswith("/retry"):
            try:
                self._retry_dead_notify(int(path[len("/api/notify/dead/"):-len("/retry")]))
            except ValueError:
                self._json({"ok": False, "error": "bad id"}, status=400)
        elif path.startswith("/api/stop/"):
            self._post_stop(path, qs)
        elif path.startswith("/api/retry/"):
            # Wave E: thin-wrap legacy path onto the full /api/tasks/.../retry handler.
            proj, task_id = self._split2(path[len("/api/retry/"):])
            self.path = f"/api/tasks/{proj}/{task_id}/retry"
            self._task_retry(proj, task_id)
            return
        elif path.startswith("/api/force/"):
            proj, task_id = self._split2(path[len("/api/force/"):])
            err = force_task(self._rconn(), proj, task_id)
            if err is None:
                self._audit("force", proj, task_id)
            self._json({"ok": err is None, "error": err})
        elif path.startswith("/api/mark-processed/"):
            self._post_mark_processed(path)
        elif path.startswith("/api/enable/"):
            proj, task_id = self._split2(path[len("/api/enable/"):])
            enabled_q = (qs.get("enabled", ["1"])[0] or "1").strip().lower()
            if enabled_q in ("0", "false", "no"):
                ok, err = pause_task(self._rconn(), proj, task_id)
                detail = "enabled=0"
            else:
                ok, err = enable_task(self._rconn(), proj, task_id)
                detail = "enabled=1"
            if ok:
                self._audit("enable", proj, task_id, detail)
            body: dict[str, Any] = {"ok": ok}
            if err:
                body["error"] = err
            self._json(body, status=(404 if err == "task not found" else 409) if not ok else 200)
        elif path.startswith("/api/priority/"):
            self._post_priority(path, qs)
        elif path.startswith("/api/project-limits/"):
            self._post_project_limits(path[len("/api/project-limits/"):])
        elif path.startswith("/api/worker/sync/"):
            self._worker_sync(path[len("/api/worker/sync/"):])
        elif path.startswith("/api/worker/file-sync/"):
            self._worker_file_sync(path[len("/api/worker/file-sync/"):])
        elif path.startswith("/api/transfer/put/"):
            rest_t = path[len("/api/transfer/put/"):]
            proj_name_t, sep_t, rel_t = rest_t.partition("/")
            if sep_t:
                self._receive_transfer_file(proj_name_t, rel_t)
            else:
                self._send_error_json(400, "Missing path")
        elif path.startswith("/api/data/upload/"):
            self._handle_data_upload(path[len("/api/data/upload/"):])
        elif path.startswith("/api/data/push/"):
            # D1: CAS blob upload — /api/data/push/<run_id>/<rel_path>
            rest_push = path[len("/api/data/push/"):]
            push_run_id, push_sep, push_rel = rest_push.partition("/")
            if push_sep:
                self._receive_cas_blob(push_run_id, push_rel)
            else:
                self._send_error_json(400, "Missing rel_path")
        elif path.startswith("/api/service/"):
            # D3: HTTP-proxy service broker (POST) — /api/service/<name>/<subpath>
            rest_svc = path[len("/api/service/"):]
            svc_name, svc_sep, svc_sub = rest_svc.partition("/")
            if svc_name:
                self._proxy_service_request(svc_name, svc_sub)
            else:
                self._send_error_json(400, "Missing service name")
        elif path.startswith("/api/audit/") and path.endswith("/rollback"):
            self._post_audit_rollback(path[len("/api/audit/"):-len("/rollback")])
        elif path.startswith("/api/runs/") and path.endswith("/rerun"):
            self._post_run_rerun(path[len("/api/runs/"):-len("/rerun")])
        elif path == "/api/tasks/bulk":
            self._post_tasks_bulk(qs)
        elif path == "/api/tasks/retry-batch":
            self._task_retry_batch()
        elif path.startswith("/api/tasks/") and path.endswith("/retry"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/retry")].partition("/")
            if sep:
                self._task_retry(proj, tid)
            else:
                self.send_error(400)
        elif path.startswith("/api/tasks/") and path.endswith("/patch"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/patch")].partition("/")
            if sep:
                self._task_patch(proj, tid)
            else:
                self.send_error(400)
        elif path.startswith("/api/tasks/") and path.endswith("/priority"):
            proj, sep, tid = path[len("/api/tasks/"):-len("/priority")].partition("/")
            if sep:
                self._task_set_priority(proj, tid)
            else:
                self.send_error(400)
        elif path.startswith("/api/project/freeze/"):
            proj_name = path[len("/api/project/freeze/"):]
            if not proj_name:
                self.send_error(400)
                return
            set_project_frozen(self._rconn(), proj_name, True)
            self._audit("freeze", proj_name, proj_name)
            self._json({"ok": True})
        elif path.startswith("/api/project/unfreeze/"):
            proj_name = path[len("/api/project/unfreeze/"):]
            if not proj_name:
                self.send_error(400)
                return
            set_project_frozen(self._rconn(), proj_name, False)
            self._audit("unfreeze", proj_name, proj_name)
            self._json({"ok": True})
        elif path.startswith("/api/project/pause/") or path.startswith("/api/project/resume/"):
            pause = path.startswith("/api/project/pause/")
            proj_name = path[len("/api/project/pause/" if pause else "/api/project/resume/"):]
            if not proj_name:
                self.send_error(400)
                return
            from taskqueue.db import set_project_paused

            set_project_paused(self._wconn(), proj_name, pause)
            self._audit("pause" if pause else "resume", proj_name, proj_name)
            self._json({"ok": True})
        elif path.startswith("/api/remove/"):
            self._post_remove_task(path, qs)
        elif path.startswith("/api/project/remove/"):
            self._post_remove_project(path[len("/api/project/remove/"):], qs)
        elif path.startswith("/api/project/stop-all/"):
            proj_name = path[len("/api/project/stop-all/"):]
            if not proj_name:
                self.send_error(400)
                return
            stopped = 0
            for row in get_running_tasks(self._rconn()):
                if row["project"] == proj_name:
                    request_stop_with_control(self._wconn(), Path(self._db_path).parent, proj_name, row["task_id"])
                    stopped += 1
            self._audit("stop_all", proj_name, proj_name, str(stopped))
            self._json({"ok": True, "stopped": stopped})
        else:
            self.send_error(404)

    def do_PUT(self) -> None:
        """W9.10: PUT /api/tasks/<project>/<task_id>/yaml — save edited YAML."""
        self._clear_stale_txn()
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/tasks/") and path.endswith("/yaml"):
            rest = path[len("/api/tasks/"):-len("/yaml")]
            proj, sep, tid = rest.partition("/")
            if sep:
                self._task_save_yaml(proj, tid)
                return
        self.send_error(404)

    def do_PATCH(self) -> None:
        self._clear_stale_txn()
        if not self._check_auth():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        sys_prefix = "/api/alerts/system/"
        if path.startswith(sys_prefix):
            key = path[len(sys_prefix):]
            if key:
                self._patch_system_alert(key)
                return
        prefix = "/api/alerts/"
        if path.startswith(prefix) and path[len(prefix):].isdigit():
            self._patch_alert(path[len(prefix):])
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        self._clear_stale_txn()
        if not self._check_auth():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        for prefix, handler in (
            ("/api/alerts/", self._delete_alert),
            ("/api/tokens/", self._delete_token),
        ):
            rest = path[len(prefix):] if path.startswith(prefix) else ""
            if rest.isdigit():
                handler(rest)
                return
        self.send_error(404)

    # ------------------------------------------------------------------
    # Extracted GET helpers (routes with non-trivial inline logic)
    # ------------------------------------------------------------------

    def _split2(self, s: str) -> tuple[str, str]:
        """Split 'project/task_id' — task_id may contain underscores, not '/'."""
        project, sep, task_id = s.partition("/")
        if not sep or not project or not task_id:
            raise BadRequest(f"expected project/task_id, got {s!r}")
        return unquote(project), unquote(task_id)

    def _read_json(self, max_bytes: int = 1024 * 1024) -> dict[str, Any] | None:
        """Parse a JSON object body. Returns None if an error response was already sent.

        F-068: never return {} after rejecting an oversized/malformed body — callers
        must treat None as "stop handling this request".
        """
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or raw_len == "":
            return {}
        try:
            content_length = int(raw_len)
        except ValueError:
            self._send_error_json(400, "Invalid Content-Length")
            return None
        if content_length < 0:
            self._send_error_json(400, "Invalid Content-Length")
            return None
        if content_length == 0:
            return {}
        # W1.5: cap JSON bodies at 1 MiB — worker payloads are tiny.
        if content_length > max_bytes:
            self._send_error_json(413, "Request body too large")
            return None
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self._send_error_json(400, "Truncated request body")
            return None
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            self._send_error_json(400, "Invalid JSON")
            return None
        if not isinstance(obj, dict):
            self._send_error_json(400, "JSON object required")
            return None
        return obj

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """API paths get a JSON error body ({ok:false, error}) instead of the stdlib HTML page."""
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        if not path.startswith("/api/") or code < 400 or code in (204, 304):
            super().send_error(code, message, explain)
            return
        try:
            short, _long = self.responses.get(code, ("Error", ""))
        except Exception:
            short = "Error"
        body = json.dumps({"ok": False, "error": message or short, "status": code}).encode()
        self.close_connection = True
        self.send_response(code, message)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)

    def end_headers(self) -> None:
        # Hardening headers on every response (API, static, SSE).
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _serve_file(self, path: Path, content_type: str) -> None:
        # Static assets revalidate (no-cache + ETag from size/mtime): a page load costs
        # one 304 per module instead of re-downloading ~40 files, and edits show at once.
        try:
            st = path.stat()
        except OSError:
            self.send_error(404)
            return
        etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", _UI_CSP)
        self.end_headers()
        self.wfile.write(data)

    def _bulk(self, op: Any) -> None:
        """Run a bulk group/suite op; a refused op becomes its 4xx JSON body (nothing was changed)."""
        from taskqueue.web.routes_write import BulkOpError

        try:
            result = op()
        except BulkOpError as exc:
            self._json(exc.payload, status=exc.status)
            return
        self._json(result)

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        enc = self.headers.get("Accept-Encoding", "")
        if "gzip" in enc.lower() and len(body) > 1024:
            import gzip
            body = gzip.compress(body, compresslevel=3)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(
        self,
        text: str,
        *,
        extra_headers: dict[str, str] | None = None,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)



def _record_request(h: "Handler", t0: float) -> None:
    """Count one API request into the usage accumulator. Fail-open, no payloads."""
    import time as _time
    from urllib.parse import urlparse

    from taskqueue import usage_track as ut

    try:
        path = urlparse(h.path).path
        if not path.startswith("/api/"):
            return
        channel = ut.request_channel(h.headers, path)
        if channel is None:
            return
        tmpl, project = ut.route_template(h.command, path)
        client = (h.headers.get("X-TQ-Client") or "").strip()
        if not client:
            client = "browser" if channel == "ui" else (h.headers.get("User-Agent") or "").split(" ")[0]
        ut.RECORDER.record(
            channel,
            f"{h.command} {tmpl}",
            client=client,
            operator=h.headers.get("X-TQ-Operator") or "",
            project=project,
            ms=(_time.monotonic() - t0) * 1000.0,
            error_code=ut.error_for_status(getattr(h, "_tq_status", None)),
        )
        ut.RECORDER.maybe_flush(h._wconn)
    except Exception:
        pass


# Exact-path routes, built once at import (was a dict of fresh lambdas per request).
# Each entry takes the request handler; prefix routes stay in do_GET / do_POST.
_GET_EXACT: dict[str, Callable[[Any], None]] = {
    "/": lambda h: h._serve_file(_STATIC / "index.html", "text/html"),
    "/index.html": lambda h: h._serve_file(_STATIC / "index.html", "text/html"),
    "/favicon.ico": lambda h: h._serve_favicon(),
    "/sw.js": lambda h: h._serve_service_worker(),
    "/manifest.webmanifest": lambda h: h._serve_file(_STATIC / "manifest.webmanifest", "application/manifest+json"),
    "/static/app.css": lambda h: h._serve_file(_STATIC / "app.css", "text/css"),
    "/static/app.js": lambda h: h._serve_file(_STATIC / "app.js", "application/javascript"),
    "/static/i18n.js": lambda h: h._serve_file(_STATIC / "i18n.js", "application/javascript"),
    "/static/theme-boot.js": lambda h: h._serve_file(_STATIC / "theme-boot.js", "application/javascript"),
    "/static/error_labels.json": lambda h: h._serve_file(_STATIC / "error_labels.json", "application/json"),
    "/static/help-bundle.en.json": lambda h: h._serve_file(_STATIC / "help-bundle.en.json", "application/json"),
    "/api/help": lambda h: h._serve_help_api(),
    "/api/status": lambda h: h._json(h._get_status()),
    "/api/health": lambda h: h._json(h._get_health()),
    "/api/dashboard": lambda h: h._get_dashboard_route(),
    "/api/queue": lambda h: h._json(h._get_queue()),
    "/api/tags": lambda h: h._get_tags_route(),
    "/api/history": lambda h: h._json(h._get_history()),
    "/api/config": lambda h: h._json(_redact_config(get_config(h._rconn()))),
    "/api/groups": lambda h: h._json(h._get_groups()),
    "/api/suites": lambda h: h._json(h._get_suites()),
    "/api/projects": lambda h: h._json(list(h._projects_ref().keys())),
    "/api/my-projects": lambda h: h._get_my_projects_route(),
    "/api/project-limits": lambda h: h._json(get_all_project_limits(h._rconn())),
    "/api/locks": lambda h: h._json(h._get_locks()),
    "/api/project-roots": lambda h: h._get_project_roots_route(),
    "/api/workers": lambda h: h._json(h._get_workers()),
    "/api/attention": lambda h: h._json(h._get_attention()),
    "/api/capacity": lambda h: h._json(build_capacity(h._rconn(), get_config(h._rconn()))),
    "/api/plan": lambda h: h._json(h._get_plan()),
    "/api/report": lambda h: h._json(h._get_report()),
    "/api/diagnose": lambda h: h._json(h._get_diagnose()),
    "/api/cost": lambda h: h._json(h._get_cost()),
    "/api/deps": lambda h: h._serve_deps(),
    "/api/notify/dead": lambda h: h._serve_notify_dead(),
    "/api/runs/diff": lambda h: h._serve_run_diff(),
    "/api/log-search": lambda h: h._serve_cross_log_search(),
    "/api/metrics": lambda h: h._serve_metrics(),
    "/api/hardware": lambda h: h._json(_get_hardware_data()),
    "/api/fits": lambda h: h._serve_fits(),
    "/api/usage": lambda h: h._json(h._get_usage()),
    "/api/task-stats": lambda h: h._get_task_stats_route(),
    "/api/agent-usage": lambda h: h._get_agent_usage_route(),
    "/api/timeline": lambda h: h._get_timeline_route(),
    "/api/schedules": lambda h: h._get_schedules_route(),
    "/api/alerts": lambda h: h._get_alerts_route(),
    "/api/alerts/system": lambda h: h._get_system_alerts_route(),
    "/api/audit": lambda h: h._get_audit_route(),
    "/api/tokens": lambda h: h._get_tokens_route(),
    "/api/notifications": lambda h: h._json(h._get_notifications()),
    "/api/events": lambda h: h._stream_sse(),
    "/api/docs": lambda h: h._serve_api_docs_html(),
    "/api/docs.json": lambda h: h._json({"endpoints": _API_REGISTRY}),
    "/api/tasks": lambda h: h._json(h._get_tasks_filtered()),
    "/api/worker/projects": lambda h: h._serve_projects_config(),
    "/api/worker/version": lambda h: h._serve_worker_version(),
    "/api/worker/wheel": lambda h: h._serve_worker_wheel(),
    "/api/worker/next-task": lambda h: h._worker_next_task(),
    "/api/worker/commands": lambda h: h._worker_commands(),
}

_POST_EXACT: dict[str, Callable[[Any], None]] = {
    "/api/config": lambda h: h._post_config(),
    "/api/cli/sync": lambda h: h._cli_sync(),
    "/api/cli/archive": lambda h: h._cli_archive(),
    "/api/cli/add": lambda h: h._cli_add(),
    "/api/validate": lambda h: h._validate_yaml(),
    "/api/worker/register": lambda h: h._worker_register(),
    "/api/worker/heartbeat": lambda h: h._worker_heartbeat(),
    "/api/worker/log-chunk": lambda h: h._worker_log_chunk(),
    "/api/worker/finalize": lambda h: h._worker_finalize(),
    "/api/worker/deregister": lambda h: h._post_worker_deregister(),
    "/api/worker/rotate-credential": lambda h: h._worker_rotate_credential(),
    "/api/alerts": lambda h: h._post_alert(),
    "/api/health/ack": lambda h: h._post_health_ack(),
    "/api/tokens": lambda h: h._post_token(),
    "/api/sse-ticket": lambda h: h._post_sse_ticket(),
    "/api/yield-master": lambda h: (
    h._rconn().execute("DELETE FROM master_lease WHERE singleton=1"),
    h._rconn().commit(),
    h._json({"ok": True, "message": "master lease yielded"}),
    ),
}


class BadRequest(ValueError):
    """Raised by request parsing helpers; the handler wrapper answers 400."""


def _instrument_handler() -> None:
    import time as _time

    for name in ("do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE"):
        orig = getattr(Handler, name)

        def make(orig=orig):
            def wrapper(self):
                t0 = _time.monotonic()
                try:
                    return orig(self)
                except BadRequest as exc:
                    self._send_error_json(400, str(exc))
                finally:
                    _record_request(self, t0)

            wrapper.__name__ = orig.__name__
            return wrapper

        setattr(Handler, name, make())


_instrument_handler()


def make_handler(
    conn: sqlite3.Connection,
    active_runs: dict,
    system_info: dict[str, Any],
    projects_ref: Callable[[], dict],
    db_path: Path,
    bind_host: str = "127.0.0.1",
):
    import threading as _threading

    from .responses import SSE_MAX_STREAMS

    ctx = HandlerContext(
        conn=conn,
        active_runs=active_runs,
        system_info=system_info,
        projects_ref=projects_ref,
        db_path=db_path,
        bind_host=bind_host,
        pending_sync=set(),
        pending_file_sync=set(),
        # Streams hold a pool thread for their whole life: cap at a quarter of the HTTP pool so
        # long-lived SSE clients can never starve ordinary REST requests.
        sse_semaphore=_threading.Semaphore(SSE_MAX_STREAMS),
        tls=_threading.local(),
    )

    class BoundHandler(Handler):
        _ctx = ctx
        _db_path = db_path
        # staticmethod: plain function class attrs become bound methods on access
        _projects_ref = staticmethod(projects_ref)
        _active_runs = active_runs
        _system_info = system_info
        _bind_host = bind_host
        _pending_sync = ctx.pending_sync
        _pending_file_sync = ctx.pending_file_sync
        _sse_semaphore = ctx.sse_semaphore  # type: ignore[assignment]
        _tls = ctx.tls
        _conn = conn
        _pending_commands = ctx.pending_commands
        _commands_condition = ctx.commands_condition

    return BoundHandler
