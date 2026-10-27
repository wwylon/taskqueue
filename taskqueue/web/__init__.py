"""Embedded HTTP server for the queue web UI.

Serves static/index.html and a JSON API. Runs in a daemon thread.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from .api_docs import (
    _API_REGISTRY,
    _build_api_docs_html,
    _register_api_docs,
    api_doc,
)
from .context import HandlerContext
from .handler import Handler, make_handler
from .hardware import _get_hardware_data, _hw_cache, _hw_lock
from .responses import (
    BoundedThreadingHTTPServer,
    _STATIC,
    _UNGROUPED,
    _apply_deps_mode,
    _claim_next_remote_task,
    _get_auth_config,
    _get_cost_response,
    _get_deps_response,
    _get_groups_response,
    _get_history_response,
    _get_queue_response,
    _get_suites_response,
    _get_usage_response,
    _history_row_dict,
    _is_loopback,
    _queue_row_dict,
    _safe_resolve,
    _task_dict,
    relativize_requires_paths,
    stream_request_body_to_file,
)
from .standby import StandbyHandler

# Re-export submodule surfaces used by tests / patches
from . import api_docs as api_docs
from . import hardware as hardware
from . import responses as responses


def start_web_server(
    conn: sqlite3.Connection,
    active_runs: dict,
    system_info: dict[str, Any],
    projects_ref: Callable[[], dict],
    db_path: Path | None = None,
    port: int = 8765,
    bind_host: str = "127.0.0.1",
) -> threading.Thread:
    if db_path is None:
        from taskqueue.config import state_home, db_path as _dbp
        db_path = _dbp(state_home())

    # W1.1: non-loopback bind requires a token — refuse to start if unconfigured.
    if not _is_loopback(bind_host):
        from taskqueue.db import get_config as _gcfg
        cfg = _gcfg(conn)
        if not (cfg.get("auth_token") or "").strip():
            raise RuntimeError(
                f"Refusing to bind to {bind_host}: auth_token is empty. "
                "Set auth_token in tq config before exposing the daemon on the network."
            )

    handler = make_handler(conn, active_runs, system_info, projects_ref, db_path, bind_host)
    # Pre-warm psutil cpu_percent baseline so first /api/hardware is not all zeros.
    try:
        import psutil as _psutil
        _psutil.cpu_percent(interval=0.1)
    except Exception:
        pass
    server = BoundedThreadingHTTPServer((bind_host, port), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def start_standby_server(
    conn: sqlite3.Connection,
    home: Path,
    *,
    port: int = 8765,
    bind_host: str = "127.0.0.1",
    promote_event: threading.Event | None = None,
) -> threading.Thread:
    """W7.3: Minimal HTTP server for standby mode.

    Serves GET /api/status, POST /api/import, and POST /api/promote.
    Does not claim the master lease or schedule tasks.
    When POST /api/promote is received, sets promote_event so run_standby
    can exit and transition to a full daemon.
    """
    if not _is_loopback(bind_host):
        from taskqueue.db import get_config as _gcfg
        cfg = _gcfg(conn)
        if not (cfg.get("auth_token") or "").strip():
            raise RuntimeError(
                f"Refusing to bind standby to {bind_host}: auth_token is empty. "
                "Set auth_token in tq config before exposing standby on the network."
            )

    _conn_box: list[sqlite3.Connection] = [conn]

    class BoundStandby(StandbyHandler):
        _conn_box = _conn_box
        _home = home
        _promote_event = promote_event

    server = BoundedThreadingHTTPServer((bind_host, port), BoundStandby)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="tq-standby-http")
    t.start()
    return t


__all__ = [
    "Handler",
    "HandlerContext",
    "StandbyHandler",
    "BoundedThreadingHTTPServer",
    "make_handler",
    "start_web_server",
    "start_standby_server",
    "api_doc",
    "_API_REGISTRY",
    "_STATIC",
    "_UNGROUPED",
    "_is_loopback",
    "_task_dict",
    "_claim_next_remote_task",
    "_get_queue_response",
    "_get_deps_response",
    "_get_suites_response",
    "_apply_deps_mode",
    "_get_cost_response",
    "_get_groups_response",
    "_get_usage_response",
    "_get_history_response",
    "_history_row_dict",
    "_queue_row_dict",
    "_safe_resolve",
    "relativize_requires_paths",
    "_get_hardware_data",
    "_build_api_docs_html",
]
