"""Handler mixin (extracted from make_handler)."""
from __future__ import annotations

# Broad shared namespace so method bodies keep working with minimal churn.
from .deps import (
    Path,
    SCHEMA_VERSION,
    UTC,
    _wheel_sha_cache,
    datetime,
    holds_master_lease,
    json,
    sqlite3,
    time,
)
from typing import Any
from taskqueue.rows import row_get

import threading as _threading

# Last accepted log-chunk ``seq`` per (db_path, run_id): a retried POST (same seq)
# must not append the same text twice. F-146: key by DB so two homes cannot collide.
_LOG_SEQ: "dict[tuple[str, int], int]" = {}
_LOG_SEQ_LOCK = _threading.Lock()
_LOG_SEQ_MAX = 4096


def _log_seq_is_duplicate(db_key: str, run_id: int, seq: "int | None") -> bool:
    if seq is None:
        return False
    with _LOG_SEQ_LOCK:
        return seq <= _LOG_SEQ.get((db_key, run_id), -1)


def _log_seq_record(db_key: str, run_id: int, seq: "int | None") -> None:
    if seq is None:
        return
    key = (db_key, run_id)
    with _LOG_SEQ_LOCK:
        if key not in _LOG_SEQ and len(_LOG_SEQ) >= _LOG_SEQ_MAX:
            for k in list(_LOG_SEQ)[: _LOG_SEQ_MAX // 2]:
                _LOG_SEQ.pop(k, None)
        _LOG_SEQ[key] = max(seq, _LOG_SEQ.get(key, -1))

# Worker long-polls park a pool thread for up to ~30 s each; cap them so a large fleet
# cannot starve the UI/API of HTTP workers (a refused long-poll degrades to a short poll).
LONGPOLL_SLOTS = _threading.BoundedSemaphore(12)


class WorkerRoutes:
    def _longpoll_budget(self, wait_sec: float) -> float:
        """Return ``wait_sec`` if a long-poll slot is free (held until the response is done), else 0."""
        if wait_sec <= 0 or getattr(self, "_lp_held", False):
            return wait_sec
        if LONGPOLL_SLOTS.acquire(blocking=False):
            self._lp_held = True
            return wait_sec
        return 0.0

    def _post_worker_deregister(self) -> None:
        data = self._read_json()
        if data is None:
            return
        worker_id = data.get("worker_id")
        if not worker_id or not isinstance(worker_id, str) or len(worker_id) > 64:
            self._json({"ok": False, "error": "Missing or invalid worker_id"}, 400)
            return
        wdb = self._open_write_conn()
        try:
            row = wdb.execute(
                "SELECT credential_hash FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            stored = str((row["credential_hash"] if row else "") or "").strip()
            if stored:
                import hmac as _hmac
                given = str(data.get("credential_hash") or "").strip()
                if not _hmac.compare_digest(stored.encode(), given.encode()):
                    self._json({"ok": False, "error": "credential_hash required and must match"}, 403)
                    return
            wdb.execute("DELETE FROM workers WHERE worker_id = ?", (worker_id,))
            wdb.commit()
        finally:
            wdb.close()
        self._json({"ok": True})

    def _check_protocol(self, data: dict[str, Any] | None = None) -> str | None:
        """Return error string if client protocol is out of supported range; None if ok."""
        from taskqueue.config import MIN_SUPPORTED_PROTOCOL, MAX_SUPPORTED_PROTOCOL

        client_proto = None
        if data is not None and data.get("protocol") is not None:
            client_proto = data.get("protocol")
        else:
            qs = self._query()
            if qs.get("protocol"):
                client_proto = qs["protocol"][0]
        if client_proto is None:
            return None  # older workers: warn-only not enforced yet
        try:
            proto = int(client_proto)
        except (TypeError, ValueError):
            return f"invalid protocol value: {client_proto!r}"
        if proto < MIN_SUPPORTED_PROTOCOL:
            return (
                f"worker protocol v{proto} too old, upgrade to at least v{MIN_SUPPORTED_PROTOCOL}"
            )
        if proto > MAX_SUPPORTED_PROTOCOL:
            return (
                f"worker protocol v{proto} too new, upgrade master (supports up to v{MAX_SUPPORTED_PROTOCOL})"
            )
        return None

    # W1.4: safe worker_id characters and max length.
    _WORKER_ID_RE = None  # lazy-compiled below

    def _serve_worker_version(self) -> None:
        """W7.5: Return master version info + upgrade URL if wheel available."""
        import hashlib
        from taskqueue import __version__ as _mv
        from taskqueue.db import SCHEMA_VERSION
        from taskqueue.config import PROTOCOL_VERSION
        wheel_path = Path(self._db_path).parent / "worker.whl"
        upgrade_url: str | None = None
        sha256: str | None = None
        if wheel_path.is_file():
            mtime = wheel_path.stat().st_mtime
            if (
                _wheel_sha_cache["sha256"] is not None
                and _wheel_sha_cache["mtime"] == mtime
            ):
                sha256 = _wheel_sha_cache["sha256"]
            else:
                h = hashlib.sha256()
                with open(wheel_path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
                sha256 = h.hexdigest()
                _wheel_sha_cache["mtime"] = mtime
                _wheel_sha_cache["sha256"] = sha256
            upgrade_url = "/api/worker/wheel"
        self._json({
            "version": _mv,
            "schema": SCHEMA_VERSION,
            "protocol": PROTOCOL_VERSION,
            "upgrade_url": upgrade_url,
            "sha256": sha256,
            "wheel_name": wheel_path.name if upgrade_url else None,
        })

    def _serve_worker_wheel(self) -> None:
        """W7.5: Serve the pre-built worker wheel."""
        import shutil
        wheel_path = Path(self._db_path).parent / "worker.whl"
        if not wheel_path.is_file():
            self._send_error_json(404, "No worker wheel available. Run: tq worker build-wheel")
            return
        size = wheel_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{wheel_path.name}"')
        self.end_headers()
        with open(wheel_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=65536)

    def _worker_register(self) -> None:
        if not self._require_master_lease():
            return
        import re
        if self.__class__._WORKER_ID_RE is None:
            self.__class__._WORKER_ID_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')

        data = self._read_json()
        if data is None:
            return
        proto_err = self._check_protocol(data)
        if proto_err:
            self._json({"ok": False, "error": proto_err}, 400)
            return
        worker_id = data.get("worker_id")
        if not worker_id:
            self._json({"ok": False, "error": "Missing worker_id"})
            return
        # W1.4: reject dangerous worker_ids (path components, Windows reserved names, too long).
        if not self._WORKER_ID_RE.match(str(worker_id)):
            self._json({"ok": False, "error": "Invalid worker_id: use [A-Za-z0-9_.-]{1,64}"}, 400)
            return
        _WIN_RESERVED = {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}
        if str(worker_id).upper() in _WIN_RESERVED:
            self._json({"ok": False, "error": f"worker_id {worker_id!r} is a reserved device name"}, 400)
            return
        os_val = data.get("os", "any")
        arch_val = data.get("arch", "any")
        # F-148: malformed numbers → 400, not 500.
        try:
            ram_gib = float(data.get("ram_gib", 0))
            cpu_cores = int(data.get("cpu_cores", 0))
            gpu_vram_gib = float(data.get("gpu_vram_gib", 0))
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "ram_gib/cpu_cores/gpu_vram_gib must be numeric"}, 400)
            return
        tags = json.dumps(data.get("tags") or [])
        projs = json.dumps(data.get("projects") or [])
        # Per-device GPU inventory (list of {index, name, vram_gib, vram_used_gib}).
        # Older workers won't send this; fall back to a single synthetic device
        # from the pooled total so the fit-check code path is uniform.
        gpus_in = data.get("gpus")
        if not gpus_in and gpu_vram_gib > 0:
            gpus_in = [{"index": 0, "name": None, "vram_gib": gpu_vram_gib, "vram_used_gib": 0.0}]
        gpus_json = json.dumps(gpus_in or [])

        from taskqueue.config import PROTOCOL_VERSION
        now = datetime.now(UTC).isoformat()

        w_version = str(data.get("version") or "") or None
        w_schema = data.get("schema")
        try:
            w_schema_int = int(w_schema) if w_schema is not None else None
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "schema must be an integer"}, 400)
            return

        client_ip = getattr(self, "client_address", ("",))[0] or ""
        session_id = data.get("session_id")
        if session_id is not None:
            session_id = str(session_id).strip() or None
        incoming_credential_hash = str(data.get("credential_hash") or "").strip() or None
        incoming_instance_id = str(data.get("worker_instance_id") or "").strip() or None

        wdb = self._open_write_conn()  # W5.4
        try:
            # F-007: check if worker_id is already online and from a different IP
            existing = wdb.execute(
                "SELECT status, registered_ip, session_id, worker_epoch, credential_hash, instance_id"
                " FROM workers WHERE worker_id = ?",
                (worker_id,)
            ).fetchone()
            import sys as _sys
            if existing and existing["status"] == "online":
                stored_ip = existing["registered_ip"] or ""
                if stored_ip and client_ip and stored_ip != client_ip:
                    print(
                        f"[web] F-007 warn: worker_id={worker_id!r} re-registered from {client_ip!r} "
                        f"(was {stored_ip!r}) while online — possible impersonation",
                        file=_sys.stderr,
                    )

            # C1: credential_hash verification (gradual migration)
            stored_cred_hash: str | None = None
            if existing is not None:
                try:
                    stored_cred_hash = existing["credential_hash"] or None
                except (KeyError, IndexError):
                    stored_cred_hash = None
            if stored_cred_hash:
                import hmac as _hmac
                if not _hmac.compare_digest(stored_cred_hash.encode(), (incoming_credential_hash or "").encode()):
                    print(
                        f"[web] C1: worker_id={worker_id!r} credential_hash mismatch — rejecting "
                        "(rotate with `tq worker rotate-credential` to change it)",
                        file=_sys.stderr,
                    )
                    self._json({"ok": False, "error": "credential_hash mismatch"}, 403)
                    return
            # Determine which credential_hash to store
            new_cred_hash = incoming_credential_hash or stored_cred_hash

            # C3: instance_id — update on each registration (changes on restart)
            stored_instance_id: str | None = None
            if existing is not None:
                try:
                    stored_instance_id = existing["instance_id"] or None
                except (KeyError, IndexError):
                    stored_instance_id = None
            if stored_instance_id and incoming_instance_id and stored_instance_id != incoming_instance_id:
                print(
                    f"[web] C3 info: worker_id={worker_id!r} instance_id changed "
                    f"({stored_instance_id!r} -> {incoming_instance_id!r}) — worker restarted",
                    file=_sys.stderr,
                )
            new_instance_id = incoming_instance_id or stored_instance_id

            # R-005: bump worker_epoch only when session_id changes (new agent process).
            prev_session = None
            prev_epoch = 0
            if existing is not None:
                if "session_id" in existing.keys():
                    prev_session = existing["session_id"]
                if "worker_epoch" in existing.keys():
                    prev_epoch = int(existing["worker_epoch"] or 0)
            if session_id is None:
                # Legacy worker: keep prior session unless IP changed (new process).
                prev_ip = ""
                if existing is not None and "registered_ip" in existing.keys():
                    prev_ip = existing["registered_ip"] or ""
                if prev_ip and client_ip and prev_ip != client_ip:
                    session_id = prev_session
                    new_epoch = prev_epoch + 1
                else:
                    session_id = prev_session
                    new_epoch = prev_epoch
            elif session_id == prev_session:
                new_epoch = prev_epoch
            else:
                new_epoch = prev_epoch + 1

            wdb.execute(
                """
                INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, tags, projects,
                    last_seen, status, gpus_json, version, schema_version, registered_ip,
                    session_id, worker_epoch, credential_hash, instance_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    os = excluded.os,
                    arch = excluded.arch,
                    ram_gib = excluded.ram_gib,
                    cpu_cores = excluded.cpu_cores,
                    gpu_vram_gib = excluded.gpu_vram_gib,
                    tags = excluded.tags,
                    projects = excluded.projects,
                    last_seen = excluded.last_seen,
                    status = 'online',
                    gpus_json = excluded.gpus_json,
                    version = excluded.version,
                    schema_version = excluded.schema_version,
                    registered_ip = excluded.registered_ip,
                    session_id = excluded.session_id,
                    worker_epoch = excluded.worker_epoch,
                    credential_hash = COALESCE(excluded.credential_hash, workers.credential_hash),
                    instance_id = excluded.instance_id
                """,
                (worker_id, os_val, arch_val, ram_gib, cpu_cores, gpu_vram_gib, tags, projs, now,
                 gpus_json, w_version, w_schema_int, client_ip, session_id, new_epoch,
                 new_cred_hash, new_instance_id)
            )
            wdb.commit()
        finally:
            wdb.close()
        resp: dict[str, Any] = {"ok": True, "protocol": PROTOCOL_VERSION}
        if session_id:
            resp["session_id"] = session_id
        resp["worker_epoch"] = new_epoch
        self._json(resp)

    def _worker_heartbeat(self) -> None:
        if not self._require_master_lease():
            return
        data = self._read_json()
        if data is None:
            return
        worker_id = data.get("worker_id")
        if not worker_id:
            self._json({"ok": False, "error": "Missing worker_id"})
            return

        from taskqueue import __version__ as _master_version
        from taskqueue.config import LEASE_TTL_SEC
        from taskqueue.db import renew_run_lease_for_worker
        from taskqueue.fencing import session_id_mismatch
        now = datetime.now(UTC).isoformat()
        stop_requested = False
        stop_run_ids: list[int] = []
        drain_and_stop = False
        drain_reason: str | None = None

        # W7.4: validate version + schema on every heartbeat
        w_version = str(data.get("version") or "")
        w_schema = data.get("schema")
        if w_version:
            try:
                wv = tuple(int(x) for x in w_version.split(".")[:3])
                mv = tuple(int(x) for x in _master_version.split(".")[:3])
                if wv[0] < mv[0]:
                    drain_and_stop = True
                    drain_reason = f"worker version {w_version} major behind master {_master_version}"
                elif wv < mv:
                    pass  # patch/minor behind — warn only (logged below)
            except Exception:
                pass
        if w_schema is not None:
            try:
                if int(w_schema) < SCHEMA_VERSION:
                    drain_and_stop = True
                    drain_reason = drain_reason or f"worker schema {w_schema} behind master {SCHEMA_VERSION}"
            except Exception:
                pass

        wdb = self._open_write_conn()  # W5.4
        try:
            row = wdb.execute(
                "SELECT session_id, instance_id FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            stored_session = None
            if row is not None and "session_id" in row.keys():
                stored_session = row["session_id"]
            if session_id_mismatch(stored_session, data.get("session_id")):
                self._json({"ok": False, "error": "session_id mismatch"}, 403)
                return
            # C3: instance_id fence — informational warning only (gradual migration)
            hb_instance_id = str(data.get("worker_instance_id") or "").strip() or None
            if hb_instance_id and row is not None:
                try:
                    stored_iid = row["instance_id"] or None
                    if stored_iid and stored_iid != hb_instance_id:
                        import sys as _sys2
                        print(
                            f"[web] C3 warn: worker_id={worker_id!r} heartbeat instance_id={hb_instance_id!r} "
                            f"!= stored {stored_iid!r} — possible stale worker",
                            file=_sys2.stderr,
                        )
                except (KeyError, IndexError):
                    pass

            load = data.get("load")
            load_json = json.dumps(load) if isinstance(load, dict) else "{}"
            wdb.execute(
                "UPDATE workers SET last_seen = ?, status = 'online', version = ?, "
                "schema_version = ?, load_json = ? WHERE worker_id = ?",
                (
                    now,
                    str(w_version) if w_version else None,
                    int(w_schema) if w_schema is not None else None,
                    load_json,
                    worker_id,
                ),
            )
            wdb.commit()
            run_ids_raw = data.get("run_ids")
            run_ids: list[int] = []
            if isinstance(run_ids_raw, list):
                for rid in run_ids_raw:
                    try:
                        run_ids.append(int(rid))
                    except (TypeError, ValueError):
                        pass
            elif data.get("run_id"):
                try:
                    run_ids.append(int(data["run_id"]))
                except (TypeError, ValueError):
                    pass
            stop_run_ids: list[int] = []
            for run_id in run_ids:
                renewed = renew_run_lease_for_worker(wdb, int(run_id), worker_id)
                if not renewed:
                    stop_run_ids.append(int(run_id))
                    continue
                row = wdb.execute(
                    "SELECT r.status, r.worker_id, t.stop_requested FROM runs r "
                    "JOIN tasks t ON t.project = r.project AND t.task_id = r.task_id "
                    "WHERE r.run_id = ?",
                    (int(run_id),),
                ).fetchone()
                if not row or row["worker_id"] != worker_id:
                    continue
                # Cooperative stop, or master already ended the run (hung reclaim)
                # while this worker still holds the child process.
                if bool(row["stop_requested"]) or row["status"] != "running":
                    stop_run_ids.append(int(run_id))
            stop_requested = bool(stop_run_ids)
        finally:
            wdb.close()
        resp: dict[str, Any] = {
            "ok": True,
            "stop_requested": stop_requested,
            "lease_ttl_sec": LEASE_TTL_SEC,
        }
        if stop_run_ids:
            resp["stop_run_ids"] = stop_run_ids
        if drain_and_stop:
            resp["drain_and_stop"] = True
            resp["reason"] = drain_reason
        # W7.8: sync request pending for this worker?
        if worker_id in self._pending_sync:
            self._pending_sync.discard(worker_id)
            resp["sync_requested"] = True
        if worker_id in self._pending_file_sync:
            self._pending_file_sync.discard(worker_id)
            resp["file_sync_requested"] = True
        self._json(resp)

    def _push_command(self, worker_id: str, cmd: dict) -> None:
        """G1: push a command to the per-worker deque and notify any waiting long-poll."""
        with self._commands_condition:
            q = self._pending_commands.setdefault(worker_id, __import__("collections").deque())
            q.append(cmd)
            self._commands_condition.notify_all()

    def _worker_sync(self, worker_id: str) -> None:
        """W7.8: enqueue a sync request for a worker. Worker picks it up on next heartbeat or commands poll."""
        if not worker_id:
            self._json({"ok": False, "error": "worker_id required"}, 400)
            return
        row = self._rconn().execute(
            "SELECT status FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if not row:
            self._json({"ok": False, "error": f"Unknown worker: {worker_id}"}, 404)
            return
        self._pending_sync.add(worker_id)
        self._push_command(worker_id, {"type": "sync"})
        self._json({"ok": True, "queued": True, "worker_id": worker_id})

    def _worker_file_sync(self, worker_id: str) -> None:
        """Queue allowlisted file sync (tq resync) for a worker."""
        if not worker_id:
            self._json({"ok": False, "error": "worker_id required"}, 400)
            return
        row = self._rconn().execute(
            "SELECT status FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if not row:
            self._json({"ok": False, "error": f"Unknown worker: {worker_id}"}, 404)
            return
        self._pending_file_sync.add(worker_id)
        self._push_command(worker_id, {"type": "file_sync"})
        self._json({"ok": True, "queued": True, "worker_id": worker_id, "kind": "file_sync"})

    def _worker_commands(self) -> None:
        """G1: long-poll endpoint for per-worker command delivery.

        GET /api/worker/commands?worker_id=X&session_id=Y&wait=25

        Returns immediately with pending commands, or holds the connection
        for up to `wait` seconds waiting for new commands to arrive. The
        heartbeat piggyback is kept as a fallback — this endpoint provides
        sub-second propagation.
        """
        import time as _time

        qs = self._query()
        worker_id = (qs.get("worker_id") or [None])[0]
        if not worker_id:
            self._json({"ok": False, "error": "Missing worker_id"}, 400)
            return
        from taskqueue.fencing import session_id_mismatch
        worker = self._rconn().execute(
            "SELECT session_id FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if not worker:
            self._json({"ok": False, "error": "Worker not registered"}, 404)
            return
        provided = (qs.get("session_id") or [None])[0]
        if session_id_mismatch(worker["session_id"], provided):
            self._json({"ok": False, "error": "session_id mismatch"}, 403)
            return

        wait_sec = 0.0
        try:
            wait_sec = float((qs.get("wait") or ["0"])[0] or 0)
        except (TypeError, ValueError):
            wait_sec = 0.0
        wait_sec = self._longpoll_budget(max(0.0, min(wait_sec, 30.0)))
        deadline = _time.monotonic() + wait_sec

        cmds: list = []
        with self._commands_condition:
            while True:
                q = self._pending_commands.get(worker_id)
                if q:
                    while q:
                        cmds.append(q.popleft())
                    break
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                self._commands_condition.wait(timeout=min(remaining, 1.0))
        # Socket write happens outside the shared condition so a slow client cannot
        # stall every other poller; undelivered commands are put back.
        try:
            self._json({"ok": True, "commands": cmds})
        except OSError:
            if cmds:
                with self._commands_condition:
                    q = self._pending_commands.setdefault(worker_id, __import__("collections").deque())
                    q.extendleft(reversed(cmds))
                    self._commands_condition.notify_all()
            raise

    def _worker_next_task(self) -> None:
        if not self._require_master_lease(next_task=True):
            return
        proto_err = self._check_protocol()
        if proto_err:
            self._json({"task": None, "error": proto_err})
            return
        qs = self._query()
        worker_id = qs.get("worker_id", [None])[0]
        if not worker_id:
            self._json({"task": None, "error": "Missing worker_id query parameter"})
            return
        worker = self._rconn().execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        if not worker:
            self._json({"task": None, "error": "Worker not registered"})
            return
        if worker["status"] == "offline":
            self._json({"task": None, "error": "Worker is offline; re-register first"})
            return
        from taskqueue.fencing import session_id_mismatch
        stored_session = row_get(worker, "session_id")
        provided = qs.get("session_id", [None])[0]
        if session_id_mismatch(stored_session, provided):
            self._json({"task": None, "error": "session_id mismatch"})
            return
        from taskqueue.config import WORK_LONGPOLL_MAX_SEC
        import time as _time
        wait_sec = 0.0
        try:
            wait_sec = float((qs.get("wait") or ["0"])[0] or 0)
        except (TypeError, ValueError):
            wait_sec = 0.0
        wait_sec = self._longpoll_budget(max(0.0, min(wait_sec, float(WORK_LONGPOLL_MAX_SEC))))
        count = 1
        try:
            count = max(1, min(8, int((qs.get("count") or ["1"])[0] or 1)))
        except (TypeError, ValueError):
            count = 1
        deadline = _time.monotonic() + wait_sec
        result: dict = {"task": None}
        while True:
            if wait_sec > 0 and not holds_master_lease(self._rconn()):
                self._json({"task": None, "error": "not master"}, 503)
                return
            # F-074: cheap read gate — do not BEGIN IMMEDIATE / service-probe
            # claim path every 0.4s while the queue is empty.
            has_queued = self._rconn().execute(
                "SELECT 1 FROM tasks WHERE status = 'queued' AND enabled = 1 LIMIT 1"
            ).fetchone()
            if has_queued:
                import taskqueue.web as _web
                if count <= 1:
                    # Patch target: taskqueue.web._claim_next_remote_task
                    result = _web._claim_next_remote_task(
                        Path(self._db_path), worker, self._projects_ref
                    )
                    if result.get("task") or result.get("error"):
                        self._send_claims(result, [result.get("run_id")])
                        return
                else:
                    claimed: list[dict] = []
                    for _ in range(count):
                        r = _web._claim_next_remote_task(
                            Path(self._db_path), worker, self._projects_ref
                        )
                        if r.get("task"):
                            claimed.append(r)
                        else:
                            break
                    if claimed:
                        self._send_claims({"tasks": claimed}, [c.get("run_id") for c in claimed])
                        return
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                self._json(result if count <= 1 else {"tasks": []})
                return
            _time.sleep(min(1.0, remaining))

    def _send_claims(self, payload: dict, run_ids: list) -> None:
        """Send a claim response; if the worker never receives it, expire the lease now so the
        next tick reclaims the run instead of leaving it stuck until the full lease TTL."""
        from taskqueue._db_runs import lease_expiry_iso

        try:
            self._json(payload)
        except OSError:
            ids = [r for r in run_ids if r]
            if ids:
                wdb = self._open_write_conn()
                try:
                    wdb.execute(
                        "UPDATE runs SET lease_expires_at = ? WHERE status = 'running' AND run_id IN (%s)"
                        % ",".join("?" * len(ids)),
                        (lease_expiry_iso(-1), *ids),
                    )
                    wdb.commit()
                finally:
                    wdb.close()
            raise

    def _worker_log_chunk(self) -> None:
        if not self._require_master_lease():
            return
        data = self._read_json()
        if data is None:
            return
        run_id = data.get("run_id")
        worker_id = data.get("worker_id")
        chunk_b64 = data.get("chunk_b64")
        if not run_id or chunk_b64 is None:
            self._json({"ok": False, "error": "Missing run_id or chunk_b64"})
            return
        if not worker_id:
            self._json({"ok": False, "error": "Missing worker_id"})
            return
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "Invalid run_id"}, 400)
            return
        if not isinstance(chunk_b64, str) or len(chunk_b64) > 8 * 1024 * 1024:
            self._json({"ok": False, "error": "chunk_b64 must be a string of at most 8 MiB"}, 413)
            return

        import base64
        from taskqueue.db import get_run_owned
        from taskqueue._db_runs import lease_expiry_iso
        try:
            chunk = base64.b64decode(chunk_b64).decode("utf-8", errors="replace")
        except Exception as e:
            self._json({"ok": False, "error": f"Invalid base64: {e}"})
            return

        run = get_run_owned(self._rconn(), int(run_id), worker_id)
        if not run or not run["log_path"]:
            self._json({"ok": False, "error": "run not owned"})
            return
        if run["status"] != "running":
            self._json({"ok": False, "error": "run not running (already finalized or reclaimed)"})
            return
        from taskqueue.fencing import session_id_mismatch
        run_session = row_get(run, "worker_session_id")
        if session_id_mismatch(run_session, data.get("session_id")):
            self._json({"ok": False, "error": "session_id mismatch"}, 403)
            return

        seq_raw = data.get("seq")
        try:
            seq = int(seq_raw) if seq_raw is not None else None
        except (TypeError, ValueError):
            seq = None
        _db_key = str(getattr(self, "_db_path", "") or "")
        if _log_seq_is_duplicate(_db_key, run_id, seq):
            self._json({"ok": True, "duplicate": True})
            return
        log_path = Path(run["log_path"])

        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        last_line = (lines[-1] if lines else (run["last_log_line"] or ""))[:2000]

        from taskqueue.runner import _parse_pct
        from taskqueue.log_issues import apply_log_lines_to_metrics_json, count_log_issues
        pct = None
        for ln in reversed(chunk.splitlines()):
            parsed = _parse_pct(ln)
            if parsed is not None:
                pct = parsed
                break
        log_hits = count_log_issues(chunk.splitlines())
        has_log_hits = sum(log_hits.values()) > 0

        wdb = self._open_write_conn()  # W5.4
        try:
            try:
                wdb.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                # Nothing written yet (file append + seq record happen below), so the
                # worker's retry of this same seq is safe and not seen as a duplicate.
                self._json({"ok": False, "error": "database busy, retry"}, 503)
                return
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(chunk)
            _log_seq_record(_db_key, run_id, seq)
            # Lease renewal rides the same write txn (a locked DB used to 500 here).
            wdb.execute(
                "UPDATE runs SET lease_expires_at = ? "
                "WHERE run_id = ? AND status = 'running' AND worker_id = ?",
                (lease_expiry_iso(None), run_id, worker_id),
            )
            new_metrics = None
            if has_log_hits or pct is not None or "TQ_METRIC" in chunk:
                from taskqueue.log_issues import apply_worker_chunk

                row_m = wdb.execute(
                    "SELECT metrics_json, progress_pct FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                new_metrics = apply_worker_chunk(
                    row_m["metrics_json"] if row_m else None,
                    chunk.splitlines(),
                    row_m["progress_pct"] if row_m else None,
                    time.time(),
                )
            wdb.execute(
                "UPDATE runs SET last_log_line = ?, "
                "progress_pct = COALESCE(?, progress_pct), "
                "metrics_json = COALESCE(?, metrics_json) "
                "WHERE run_id = ?",
                (last_line, pct, new_metrics, run_id),
            )
            wdb.commit()
        except Exception:
            try:
                wdb.rollback()
            except Exception:
                pass
            raise
        finally:
            wdb.close()

        # T-200 / ds2 §1.7: piggyback stop on log-chunk (faster than heartbeat alone).
        resp: dict = {"ok": True}
        try:
            row = self._rconn().execute(
                "SELECT r.status, r.worker_id, t.stop_requested FROM runs r "
                "JOIN tasks t ON t.project = r.project AND t.task_id = r.task_id "
                "WHERE r.run_id = ?",
                (int(run_id),),
            ).fetchone()
            if row is not None and row["worker_id"] == worker_id:
                if bool(row["stop_requested"]) or row["status"] != "running":
                    resp["stop_run_ids"] = [int(run_id)]
                    resp["stop_requested"] = True
        except Exception:
            pass
        self._json(resp)

    def _worker_finalize(self) -> None:
        if not self._require_master_lease():
            return
        from taskqueue import errors as tqerr
        from taskqueue.auth import finalize_token_matches
        from taskqueue.db import finalize_run_and_task, get_config, get_run_owned
        from taskqueue.notify import emit_task_notify

        data = self._read_json()
        if data is None:
            return
        run_id = data.get("run_id")
        worker_id = data.get("worker_id")
        if not run_id:
            self._json({"ok": False, "error": "Missing run_id"})
            return
        if not worker_id:
            self._json({"ok": False, "error": "Missing worker_id"})
            return
        try:
            exit_code = int(data.get("exit_code", -1))
            peak_ram = float(data.get("peak_ram_gib", 0.0) or 0.0)
            peak_vram = float(data.get("peak_vram_gib", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "exit_code/peak_* must be numeric"}, 400)
            return
        status = data.get("status", "failed")
        stop_reason = data.get("stop_reason")
        if stop_reason is not None:
            stop_reason = str(stop_reason)

        notify_payload = None
        # F-009: open_db() runs full schema migration on every finalize (expensive).
        # Use a short-lived write connection instead — schema is already current.
        finalize_token = data.get("finalize_token")
        wdb = self._open_write_conn()
        try:
            run = get_run_owned(wdb, int(run_id), worker_id)
            if not run:
                self._json({"ok": False, "error": "run not owned"})
                return
            if run["status"] != "running":
                self._json({"ok": False, "error": "run not running (already finalized or reclaimed)"})
                return
            from taskqueue.fencing import session_id_mismatch
            run_session = row_get(run, "worker_session_id")
            if session_id_mismatch(run_session, data.get("session_id")):
                self._json({"ok": False, "error": "session_id mismatch"}, 403)
                return
            # F-055: verify per-claim token if present (NULL means pre-F-055 run — allow)
            stored_token = row_get(run, "finalize_token")
            if stored_token and not finalize_token_matches(stored_token, finalize_token):
                self._json({"ok": False, "error": "finalize_token mismatch"})
                return
            proj, tid = run["project"], run["task_id"]
            err_code = None
            stored_sr = row_get(run, "stop_reason")
            if stored_sr == "deps_invalidated" or stop_reason == "deps_invalidated":
                status = "stopped"
                err_code = tqerr.STOP_DEPS
                stop_reason = "deps_invalidated"
            elif stop_reason == "wall_clock_cap" or stored_sr == "wall_clock_cap":
                status = "interrupted"
                err_code = tqerr.RUN_CAP
                stop_reason = "wall_clock_cap"
            elif status == "failed" and exit_code is not None and exit_code != 0:
                from taskqueue.daemon import is_negative_exit
                from taskqueue.db import get_task as _get_task

                if is_negative_exit(_get_task(wdb, proj, tid), exit_code):
                    status = "completed"
                    err_code = tqerr.RUN_NEGATIVE
                else:
                    err_code = tqerr.RUN_NONZERO
            updated = finalize_run_and_task(
                wdb,
                int(run_id),
                proj,
                tid,
                exit_code,
                status,
                last_error_code=err_code,
                peak_ram_gib=float(peak_ram or 0.0),
                peak_vram_gib=float(peak_vram or 0.0),
                stop_reason=stop_reason,
                stopped_by=(
                    "tq"
                    if stop_reason in (
                        "deps_invalidated", "wall_clock_cap",
                        "ram_hard", "vram_hard", "frozen", "frozen_restart", "evicted",
                    )
                    else None
                ),
                only_if_running=True,
                clear_task_error=(err_code is None and status == "completed"),
            )
            if not updated:
                self._json({"ok": False, "error": "run not running (already finalized or reclaimed)"})
                return
            _sys_stopped_by = (
                "tq"
                if stop_reason in (
                    "deps_invalidated", "wall_clock_cap",
                    "ram_hard", "vram_hard", "frozen", "frozen_restart", "evicted",
                )
                else None
            )
            if err_code == tqerr.STOP_DEPS:
                from taskqueue.db import set_task_error
                prev = wdb.execute(
                    "SELECT block_reason FROM tasks WHERE project = ? AND task_id = ?",
                    (proj, tid),
                ).fetchone()
                br = prev["block_reason"] if prev else None
                set_task_error(wdb, proj, tid, err_code, block_reason=br)
            elif err_code == tqerr.RUN_CAP:
                from taskqueue.db import apply_cap_auto_retry, set_task_error

                set_task_error(wdb, proj, tid, err_code, block_reason=None)
                outcome = apply_cap_auto_retry(wdb, proj, tid)
                if outcome == "queued":
                    status = "queued"
            task = wdb.execute(
                "SELECT notify_url, started_by FROM tasks "
                "WHERE project = ? AND task_id = ?",
                (proj, tid),
            ).fetchone()
            allow_remote = bool(get_config(wdb).get("notify_allow_remote"))
            # Capture notify payload before follow-up UPDATEs so a later
            # failure cannot silently drop end-of-task notification.
            notify_payload = (
                Path(self._db_path).parent,
                proj,
                tid,
                status,
                int(run_id),
                exit_code,
                err_code,
                task["notify_url"] if task else None,
                task["started_by"] if task else None,
                allow_remote,
                _sys_stopped_by,
                stop_reason,
            )
            # Wake hooks parity with local runner finalize
            try:
                from taskqueue.finalize_hooks import apply_finalize_wake_hooks

                apply_finalize_wake_hooks(
                    wdb, proj, tid, status, run_teardown=True
                )
            except Exception:
                pass
            try:
                from taskqueue.system_alerts import on_run_terminal

                on_run_terminal(
                    wdb,
                    Path(self._db_path).parent,
                    project=proj,
                    task_id=tid,
                    status=status,
                    run_id=int(run_id),
                    last_error_code=err_code,
                    stopped_by=_sys_stopped_by,
                    stop_reason=stop_reason,
                )
            except Exception:
                pass
        finally:
            wdb.close()
        if notify_payload:
            home, proj, tid, st, rid, ec, err, nurl, sby, allow, _sb, _sr = notify_payload
            emit_task_notify(
                home,
                project=proj,
                task_id=tid,
                status=st,
                run_id=rid,
                exit_code=ec,
                last_error_code=err,
                notify_url=nurl,
                started_by=sby,
                allow_remote=allow,
            )
        self._json({"ok": True})

    def _worker_rotate_credential(self) -> None:
        """C1: rotate a worker's per-worker credential token."""
        data = self._read_json()
        if data is None:
            return
        worker_id = data.get("worker_id")
        if not worker_id:
            self._json({"ok": False, "error": "Missing worker_id"}, 400)
            return
        old_hash = str(data.get("old_credential_hash") or "").strip() or None
        new_hash = str(data.get("new_credential_hash") or "").strip() or None
        if not new_hash:
            self._json({"ok": False, "error": "Missing new_credential_hash"}, 400)
            return
        wdb = self._open_write_conn()
        try:
            row = wdb.execute(
                "SELECT credential_hash FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if row is None:
                self._json({"ok": False, "error": "Worker not found"}, 404)
                return
            stored_hash = str(row["credential_hash"] or "").strip() or None
            import hmac as _hmac
            if stored_hash and not (
                old_hash and _hmac.compare_digest(stored_hash.encode(), old_hash.encode())
            ):
                import sys as _sys3
                print(
                    f"[web] C1 warn: rotate-credential for {worker_id!r} — "
                    "old_credential_hash missing or mismatched; rejecting",
                    file=_sys3.stderr,
                )
                self._json({"ok": False, "error": "old_credential_hash required and must match"}, 403)
                return
            wdb.execute(
                "UPDATE workers SET credential_hash = ? WHERE worker_id = ?",
                (new_hash, worker_id),
            )
            wdb.commit()
        finally:
            wdb.close()
        self._json({"ok": True})
