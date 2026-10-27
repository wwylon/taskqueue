"""Worker credential enforcement (register / rotate / deregister) and SSE semaphore release."""
from __future__ import annotations

import threading
from pathlib import Path

from tests.helpers.master_worker import http_request, make_handler_env

_REG = {"os": "linux", "arch": "amd64", "ram_gib": 8, "cpu_cores": 4}


def _register(h, wid, cred=None, session="s1"):
    body = {"worker_id": wid, "session_id": session, **_REG}
    if cred is not None:
        body["credential_hash"] = cred
    return http_request(h, "POST", "/api/worker/register", body)


def test_register_rejects_missing_or_wrong_hash(tmp_path: Path) -> None:
    h, _db, _ = make_handler_env(tmp_path)
    assert _register(h, "w1", "aaa")[0] == 200
    assert _register(h, "w1", "bbb", "s2")[0] == 403
    assert _register(h, "w1", None, "s3")[0] == 403
    assert _register(h, "w1", "aaa", "s4")[0] == 200


def test_rotate_requires_old_hash(tmp_path: Path) -> None:
    h, _db, _ = make_handler_env(tmp_path)
    _register(h, "w1", "aaa")
    path = "/api/worker/rotate-credential"
    code, _ = http_request(h, "POST", path, {"worker_id": "w1", "new_credential_hash": "ccc"})
    assert code == 403
    code, _ = http_request(
        h, "POST", path,
        {"worker_id": "w1", "old_credential_hash": "zzz", "new_credential_hash": "ccc"},
    )
    assert code == 403


def test_deregister_requires_credential(tmp_path: Path) -> None:
    h, db, _ = make_handler_env(tmp_path)
    _register(h, "w1", "aaa")
    path = "/api/worker/deregister"
    assert http_request(h, "POST", path, {"worker_id": "w1"})[0] == 403
    assert http_request(h, "POST", path, {"worker_id": "w1", "credential_hash": "bad"})[0] == 403
    assert db.execute("SELECT 1 FROM workers WHERE worker_id='w1'").fetchone() is not None
    assert http_request(h, "POST", path, {"worker_id": "w1", "credential_hash": "aaa"})[0] == 200
    assert db.execute("SELECT 1 FROM workers WHERE worker_id='w1'").fetchone() is None


def test_sse_semaphore_released_on_body_error() -> None:
    from taskqueue.web.sse import SSEMixin  # type: ignore[attr-defined]

    class H(SSEMixin):
        _sse_semaphore = threading.BoundedSemaphore(1)

        def _stream_log_sse_body(self, run_id):
            raise ConnectionResetError

        def _stream_sse_body(self):
            raise BrokenPipeError

    h = H()
    h._stream_log_sse(1)
    h._stream_sse()
    assert h._sse_semaphore.acquire(blocking=False)


def test_legacy_data_file_confined_to_data_dir(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "data" / "ok.txt").write_text("hi")
    (root / ".env").write_text("SECRET=1")

    class _P:
        pass

    p = _P()
    p.root = root
    h, _db, _ = make_handler_env(tmp_path, projects={"proj": p})
    code, _ = http_request(h, "GET", "/api/data/file/proj/data/ok.txt")
    assert code == 200
    code, _ = http_request(h, "GET", "/api/data/file/proj/.env")
    assert code == 403


def test_content_length_helper_clamps_bad_values() -> None:
    from taskqueue.web.routes_transfer import TransferRoutes  # noqa

    class H(TransferRoutes):
        headers: dict = {}

    h = H()
    for raw, want in (("12", 12), ("-5", 0), ("abc", 0), (None, 0)):
        h.headers = {} if raw is None else {"Content-Length": raw}
        assert h._content_length() == want


def test_log_chunk_rejects_bad_run_id_and_oversize(tmp_path: Path) -> None:
    h, _db, _ = make_handler_env(tmp_path)
    p = "/api/worker/log-chunk"
    code, _ = http_request(h, "POST", p, {"run_id": "abc", "worker_id": "w", "chunk_b64": "aGk="})
    assert code == 400
    code, _ = http_request(
        h, "POST", p, {"run_id": 1, "worker_id": "w", "chunk_b64": "A" * (8 * 1024 * 1024 + 1)}
    )
    assert code == 413


def test_commands_requeued_when_write_fails(tmp_path: Path) -> None:
    import collections

    h, _db, _ = make_handler_env(tmp_path)
    _register(h, "w1", "aaa")
    from taskqueue.web import routes_worker

    from taskqueue.web.handler import Handler

    class Fake(routes_worker.WorkerRoutes):
        _query = Handler._query

    obj = Fake.__new__(Fake)
    obj._commands_condition = threading.Condition()
    obj._pending_commands = {"w1": collections.deque([{"c": 1}, {"c": 2}])}
    obj.path = "/api/worker/commands?worker_id=w1"
    obj._rconn = lambda: _db
    obj._json = lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError())
    try:
        obj._worker_commands()
    except BrokenPipeError:
        pass
    assert list(obj._pending_commands["w1"]) == [{"c": 1}, {"c": 2}]
