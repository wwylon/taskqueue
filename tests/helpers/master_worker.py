"""Master–worker test harness: seed data, HTTP fakes, mini-cluster."""

from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import parse_headers
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

from taskqueue.config import PROTOCOL_VERSION, Project
from taskqueue.db import open_db
from taskqueue.scheduler import (
    FilterContext,
    filter_task,
    node_from_worker,
    should_schedule_locally,
)
from taskqueue._db_tasks import count_online_workers
from taskqueue.web import make_handler


def seed_master_lease(db, holder: str | None = None, ttl: int = 90) -> str:
    import time

    name = holder if holder is not None else socket.gethostname()
    db.execute(
        "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at) VALUES (1, ?, ?)",
        (name, int(time.time()) + ttl),
    )
    db.commit()
    return name


def clear_master_lease(db) -> None:
    db.execute("DELETE FROM master_lease WHERE singleton=1")
    db.commit()


def seed_worker(
    db,
    worker_id: str = "w1",
    *,
    ram_gib: float = 16.0,
    cpu_cores: int = 8,
    gpu_vram_gib: float = 0.0,
    projects: list[str] | None = None,
    status: str = "online",
    load: dict | None = None,
    session_id: str | None = "sess-1",
    os: str = "windows",
    arch: str = "x86_64",
) -> Any:
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen, load_json, session_id, worker_epoch) "
        "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 1)",
        (
            worker_id,
            os,
            arch,
            ram_gib,
            cpu_cores,
            gpu_vram_gib,
            json.dumps(projects or ["proj"]),
            status,
            now,
            json.dumps(load or {}),
            session_id,
        ),
    )
    db.commit()
    return db.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()


def seed_queued_task(
    db,
    *,
    project: str = "proj",
    task_id: str = "T1",
    dispatch: str = "remote",
    priority: int = 50,
    resources: dict | None = None,
    execution_semantics: str = "",
    idempotent: int = 0,
    transfer: list | None = None,
    requires: str = "{}",
    enabled: int = 1,
) -> None:
    now = datetime.now(UTC).isoformat()
    res = json.dumps(resources or {"cpu_cores": 1, "ram_gib": 1.0, "gpu_vram_gib": 0.0})
    xfer = json.dumps(transfer or [])
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, max_attempts, enabled, enqueued_at, updated_at, "
        "execution_semantics, idempotent, transfer, requires) "
        "VALUES (?, ?, 'queued', ?, '[]', ?, ?, ?, '[]', 0, 0, ?, ?, ?, ?, ?, ?, ?)",
        (
            project,
            task_id,
            "true",
            dispatch,
            priority,
            res,
            enabled,
            now,
            now,
            execution_semantics,
            idempotent,
            xfer,
            requires,
        ),
    )
    db.commit()


def make_handler_env(
    tmp_path: Path,
    projects: dict | None = None,
) -> tuple[type, Any, Path]:
    home = tmp_path
    db_path = home / "queue.db"
    db = open_db(db_path)
    seed_master_lease(db)

    def projects_ref():
        return projects or {}

    HandlerClass = make_handler(db, {}, {}, projects_ref, db_path, "127.0.0.1")
    return HandlerClass, db, home


class _FakeWfile:
    def __init__(self) -> None:
        self.data = b""

    def write(self, d: bytes) -> None:
        self.data += d

    def flush(self) -> None:
        pass


def http_request(
    HandlerClass: type,
    method: str,
    path: str,
    body: bytes | dict | None = None,
    *,
    extra_headers: dict | None = None,
) -> tuple[int | None, dict | bytes]:
    if isinstance(body, dict):
        body_bytes = json.dumps(body).encode("utf-8")
        hdrs = {"Content-Type": "application/json", **(extra_headers or {})}
    elif body is None:
        body_bytes = b""
        hdrs = dict(extra_headers or {})
    else:
        body_bytes = body
        hdrs = dict(extra_headers or {})

    content_length = hdrs.get("Content-Length", str(len(body_bytes)))
    headers_raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:8765\r\n"
        f"Content-Length: {content_length}\r\n"
    )
    for k, v in hdrs.items():
        if k.lower() == "content-length":
            continue
        headers_raw += f"{k}: {v}\r\n"
    headers_raw += "\r\n"
    raw = headers_raw.encode() + body_bytes

    wfile = _FakeWfile()
    status: list[int] = []

    class TestHandler(HandlerClass):
        def setup(self) -> None:
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
            self.client_address = ("127.0.0.1", 9)

        def send_response(self, code: int, msg: str | None = None) -> None:
            status.append(code)

        def send_header(self, k: str, v: str) -> None:
            pass

        def end_headers(self) -> None:
            pass

        def log_message(self, *a: Any) -> None:
            pass

    h = TestHandler.__new__(TestHandler)
    TestHandler.setup(h)
    h.handle_one_request()
    code = status[0] if status else None
    body_out = wfile.data
    if body_out.startswith(b"HTTP/"):
        parts = body_out.split(b"\r\n\r\n", 1)
        payload = parts[1] if len(parts) > 1 else b""
    else:
        payload = body_out
    if payload.startswith((b"{", b"[")):
        try:
            return code, json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            pass
    return code, payload


def local_dispatch_allowed(db, dispatch: str) -> bool:
    return should_schedule_locally(dispatch, online_workers=count_online_workers(db))


def remote_claim_allowed(db, worker_row: Any, task_row: Any) -> bool:
    node = node_from_worker(worker_row)
    ctx = FilterContext(
        online_workers=count_online_workers(db),
        dep_status=lambda p, t: None,
    )
    cpu, ram, gpu = 1.0, 1.0, 0.0
    return filter_task(task_row, node, ctx, cpu=cpu, ram=ram, gpu=gpu).ok


@dataclass
class MiniCluster:
    home: Path
    db: Any
    HandlerClass: type
    port: int
    base_url: str
    server: ThreadingHTTPServer
    thread: threading.Thread

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2.0)
        self.db.close()


class FakeWorkerClient:
    def __init__(self, base_url: str, worker_id: str = "w1", session_id: str = "sess-1") -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.session_id = session_id

    def _json_request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 10.0
    ) -> tuple[int, dict | None]:
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return e.code, None

    def register(self, projects: list[str] | None = None) -> dict | None:
        _, data = self._json_request(
            "POST",
            "/api/worker/register",
            {
                "worker_id": self.worker_id,
                "session_id": self.session_id,
                "protocol": PROTOCOL_VERSION,
                "os": "windows",
                "arch": "x86_64",
                "ram_gib": 16.0,
                "cpu_cores": 8,
                "gpu_vram_gib": 0.0,
                "projects": projects or ["proj"],
            },
        )
        return data

    def claim(self, wait: int = 0) -> dict | None:
        _, data = self._json_request(
            "GET",
            f"/api/worker/next-task?worker_id={self.worker_id}"
            f"&session_id={self.session_id}&protocol={PROTOCOL_VERSION}&wait={wait}",
            timeout=float(max(wait, 0)) + 15.0,
        )
        return data

    def heartbeat(self, run_ids: list[int] | None = None) -> dict | None:
        body: dict[str, Any] = {
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "protocol": PROTOCOL_VERSION,
        }
        if run_ids:
            body["run_ids"] = run_ids
        _, data = self._json_request("POST", "/api/worker/heartbeat", body)
        return data

    def log_chunk(self, run_id: int, text: str) -> dict | None:
        import base64

        _, data = self._json_request(
            "POST",
            "/api/worker/log-chunk",
            {
                "run_id": run_id,
                "worker_id": self.worker_id,
                "session_id": self.session_id,
                "chunk_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            },
        )
        return data

    def finalize(
        self,
        run_id: int,
        finalize_token: str,
        *,
        status: str = "completed",
        exit_code: int = 0,
    ) -> dict | None:
        _, data = self._json_request(
            "POST",
            "/api/worker/finalize",
            {
                "run_id": run_id,
                "worker_id": self.worker_id,
                "session_id": self.session_id,
                "finalize_token": finalize_token,
                "status": status,
                "exit_code": exit_code,
            },
        )
        return data


@contextmanager
def mini_cluster(
    tmp_path: Path,
    *,
    projects: dict | None = None,
    lease_ttl: int = 90,
    monkeypatch=None,
) -> Iterator[MiniCluster]:
    home = tmp_path
    if monkeypatch is not None:
        monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    db_path = home / "queue.db"
    db = open_db(db_path)
    seed_master_lease(db, ttl=lease_ttl)

    def projects_ref() -> dict:
        return projects or {}

    HandlerClass = make_handler(db, {}, {}, projects_ref, db_path, "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), HandlerClass)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cluster = MiniCluster(
        home=home,
        db=db,
        HandlerClass=HandlerClass,
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        server=server,
        thread=thread,
    )
    try:
        yield cluster
    finally:
        cluster.close()
