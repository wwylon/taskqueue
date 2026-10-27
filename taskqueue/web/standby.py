"""Standby-mode minimal HTTP handler."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .responses import stream_request_body_to_file


class StandbyHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for standby mode. Bound via class attrs."""

    _conn_box: list[sqlite3.Connection]
    _home: Path
    _promote_event: threading.Event | None

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_standby_auth(self) -> bool:
        """F-053: enforce the same auth_token as the main daemon (timing-safe)."""
        import hmac
        from taskqueue.auth import extract_request_token

        try:
            row = type(self)._conn_box[0].execute(
                "SELECT value FROM config WHERE key='auth_token'"
            ).fetchone()
            token = json.loads(row[0]) if row else ""
        except Exception:
            token = ""
        if not token:
            return True
        provided = extract_request_token(dict(self.headers))
        if not provided:
            return False
        return hmac.compare_digest(provided, str(token))

    def _row_counts(self) -> dict[str, int]:
        try:
            c = type(self)._conn_box[0]
            tasks = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            schema_row = c.execute(
                "SELECT value FROM config WHERE key='schema_version'"
            ).fetchone()
            schema_v = int(schema_row[0]) if schema_row else 0
            return {"tasks_count": tasks, "runs_count": runs, "schema_version": schema_v}
        except Exception:
            return {}

    def do_GET(self) -> None:
        if not self._check_standby_auth():
            self._send_json(401, {"error": "Unauthorized"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/status":
            row = type(self)._conn_box[0].execute(
                "SELECT holder, expires_at FROM master_lease WHERE singleton=1"
            ).fetchone()
            self._send_json(200, {
                "mode": "standby",
                "master_lease": dict(row) if row else None,
                "db": str(type(self)._home / "queue.db"),
                **self._row_counts(),
            })
        else:
            self._send_json(
                404,
                {"error": "standby mode: only /api/status, POST /api/import, POST /api/promote"},
            )

    def do_POST(self) -> None:
        if not self._check_standby_auth():
            self._send_json(401, {"error": "Unauthorized"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/import":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 512 * 1024 * 1024:
                    self._send_json(
                        400,
                        {"error": "Content-Length missing or too large (max 512 MiB)"},
                    )
                    return
                fd, tmp_name = tempfile.mkstemp(prefix="tq-import-", suffix=".tar.gz")
                os.close(fd)
                tmp_path = Path(tmp_name)
                from taskqueue.export_import import import_home
                from taskqueue.db import open_db as _open_db
                try:
                    stream_request_body_to_file(self.rfile, length, tmp_path)
                    info = import_home(type(self)._home, tmp_path, force=True)
                finally:
                    tmp_path.unlink(missing_ok=True)
                try:
                    type(self)._conn_box[0].close()
                except Exception:
                    pass
                type(self)._conn_box[0] = _open_db(type(self)._home / "queue.db")
                self._send_json(200, {"ok": True, "imported": info})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
        elif path == "/api/promote":
            ev = type(self)._promote_event
            if ev is not None:
                ev.set()
            self._send_json(
                200,
                {"ok": True, "message": "promote signal sent; standby will claim master lease"},
            )
        else:
            self._send_json(
                404,
                {"error": "standby: only /api/import POST and /api/promote POST are supported"},
            )
