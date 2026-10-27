"""Aggregated interaction counters (``api_usage``): who calls what, how often, how fast.

Hourly buckets, never events: no args, payloads, logs or tokens are stored. The
``op`` dimension is always a template (MCP tool name / API route template), so
cardinality stays bounded. Recording is fail-open and can be disabled with
``tq config set usage_tracking=0``. Identity (client/operator) is self-reported.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

MAX_KEYS = 4000  # in-memory distinct keys before overflow collapses into op="other"
FLUSH_INTERVAL_SEC = 60.0
DEFAULT_RETENTION_DAYS = 400


def bucket_now(now: datetime | None = None) -> str:
    n = now or datetime.now(UTC)
    return n.strftime("%Y-%m-%dT%H:00:00Z")


def _clip(v: str | None, n: int = 64) -> str:
    return (v or "").strip()[:n]


def _upsert(conn: sqlite3.Connection, rows: list[tuple[tuple, list[int]]]) -> None:
    for key, (calls, total_ms, max_ms) in rows:
        conn.execute(
            "INSERT INTO api_usage (bucket_ts, channel, client, operator, project, op, error_code, "
            "calls, total_ms, max_ms) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(bucket_ts, channel, client, operator, project, op, error_code) DO UPDATE SET "
            "calls = calls + excluded.calls, total_ms = total_ms + excluded.total_ms, "
            "max_ms = MAX(max_ms, excluded.max_ms)",
            (*key, calls, total_ms, max_ms),
        )


class UsageRecorder:
    """Thread-safe in-memory accumulator, flushed to ``api_usage`` at most once a minute."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._acc: dict[tuple, list[int]] = {}
        self._last_flush = time.monotonic()
        self.enabled = True

    def record(
        self,
        channel: str,
        op: str,
        *,
        client: str = "",
        operator: str = "",
        project: str = "",
        ms: float = 0.0,
        error_code: str = "",
    ) -> None:
        if not self.enabled:
            return
        ms_i = max(0, int(ms))
        key = (bucket_now(), channel, _clip(client), _clip(operator), _clip(project), _clip(op, 96), _clip(error_code, 48))
        with self._lock:
            slot = self._acc.get(key)
            if slot is None:
                if len(self._acc) >= MAX_KEYS:
                    key = (*key[:5], "other", key[6])
                    slot = self._acc.get(key)
                if slot is None:
                    slot = self._acc[key] = [0, 0, 0]
            slot[0] += 1
            slot[1] += ms_i
            slot[2] = max(slot[2], ms_i)

    def flush(self, conn: sqlite3.Connection) -> int:
        """Write and clear the accumulator. On DB failure keep the data (merge back)."""
        with self._lock:
            acc, self._acc = self._acc, {}
            self._last_flush = time.monotonic()
        if not acc:
            return 0
        rows = list(acc.items())
        try:
            _upsert(conn, rows)
            conn.commit()
        except sqlite3.Error:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            with self._lock:
                for key, (c, t, m) in rows:
                    slot = self._acc.setdefault(key, [0, 0, 0])
                    slot[0] += c
                    slot[1] += t
                    slot[2] = max(slot[2], m)
            return 0
        return len(rows)

    def maybe_flush(self, get_conn: Callable[[], sqlite3.Connection], interval: float = FLUSH_INTERVAL_SEC) -> None:
        """Flush if ``interval`` elapsed; also refreshes ``enabled`` from config. Never raises."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_flush < interval:
                return
            self._last_flush = now  # claim the slot so concurrent requests skip
        try:
            conn = get_conn()
            from taskqueue.db import get_config

            self.enabled = int(get_config(conn).get("usage_tracking", 1)) != 0
            if self.enabled:
                self.flush(conn)
        except Exception:
            pass


RECORDER = UsageRecorder()


def record_direct(
    conn: sqlite3.Connection,
    channel: str,
    op: str,
    *,
    client: str = "",
    operator: str = "",
    project: str = "",
    ms: float = 0.0,
    error_code: str = "",
) -> None:
    """Immediate upsert for low-volume callers (the MCP process). Fail-open."""
    try:
        from taskqueue.db import get_config

        if int(get_config(conn).get("usage_tracking", 1)) == 0:
            return
        ms_i = max(0, int(ms))
        key = (bucket_now(), channel, _clip(client), _clip(operator), _clip(project), _clip(op, 96), _clip(error_code, 48))
        _upsert(conn, [(key, [1, ms_i, ms_i])])
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


# --- route templating ------------------------------------------------------

_ROUTES: list[tuple[str, re.Pattern[str], str]] | None = None


def _routes() -> list[tuple[str, re.Pattern[str], str]]:
    global _ROUTES
    if _ROUTES is None:
        from taskqueue.web.api_docs import _API_REGISTRY

        built = []
        for e in _API_REGISTRY:
            path = str(e["path"])
            rx = re.sub(r"<([A-Za-z_]+)>", lambda m: f"(?P<{m.group(1)}>[^/]+)", re.escape(path).replace("\\<", "<").replace("\\>", ">"))
            built.append((str(e["method"]).upper(), re.compile("^" + rx + "$"), path))
        # most specific (fewest placeholders, longest) first
        built.sort(key=lambda t: (t[2].count("<"), -len(t[2])))
        _ROUTES = built
    return _ROUTES


def route_template(method: str, path: str) -> tuple[str, str]:
    """Map a concrete request path to ``(template, project)``; unknown -> ``("other", "")``."""
    method = method.upper()
    for m, rx, tmpl in _routes():
        if m == method:
            hit = rx.match(path)
            if hit:
                return tmpl, hit.groupdict().get("project", "") or ""
    return "other", ""


def error_for_status(status: int | None) -> str:
    if status is None or status < 400:
        return ""
    return f"http.{status}"


def prune_api_usage(conn: sqlite3.Connection, keep_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete buckets older than ``keep_days``. Returns rows deleted."""
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(keep_days)))).strftime("%Y-%m-%dT%H:00:00Z")
    cur = conn.execute("DELETE FROM api_usage WHERE bucket_ts < ?", (cutoff,))
    conn.commit()
    return int(cur.rowcount or 0)


def request_channel(headers: Any, path: str) -> str | None:
    """Channel for an HTTP request, or ``None`` to skip counting (MCP counts itself)."""
    via = (headers.get("X-TQ-Via") or "").strip().lower()
    if via in ("mcp", "cli"):
        return None  # those processes count their own top-level calls
    if via == "api":
        return via
    if path.startswith("/api/worker"):
        return "worker"
    if headers.get("Sec-Fetch-Site") or headers.get("Sec-Fetch-Mode"):
        return "ui"
    return "http"
