import os
import sys
import time
import json
import uuid
import base64
import platform
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from taskqueue.reap import terminate_tree
import psutil

from taskqueue.config import (
    PROTOCOL_VERSION,
    MIN_SUPPORTED_PROTOCOL,
    MAX_SUPPORTED_PROTOCOL,
    WORK_LONGPOLL_MAX_SEC,
    load_projects,
)
from taskqueue import __version__ as _WORKER_VERSION
from taskqueue.db import SCHEMA_VERSION as _WORKER_SCHEMA

_AUTH_TOKEN: str = ""


def set_auth_token(token: str | None) -> None:
    global _AUTH_TOKEN
    _AUTH_TOKEN = (token or os.environ.get("TQ_AUTH_TOKEN") or "").strip()


def _auth_headers(extra: dict | None = None) -> dict:
    from taskqueue.auth import client_auth_headers

    return client_auth_headers(_AUTH_TOKEN, extra=extra)


def _post(url: str, data: dict, *, timeout: float = 30) -> dict | None:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=_auth_headers({"Content-Type": "application/json"}),
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _post._auth_fail_streak = 0
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Count consecutive auth failures to distinguish transient proxy 403s from
            # a permanently invalid token.  Only exit after repeated failures.
            _post._auth_fail_streak = getattr(_post, "_auth_fail_streak", 0) + 1
            if _post._auth_fail_streak >= 3:
                print(
                    f"[worker] FATAL: authentication rejected by master (HTTP {e.code}) "
                    f"{_post._auth_fail_streak} times in a row. Check TQ_AUTH_TOKEN. Exiting.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(
                f"[worker] auth error HTTP {e.code} (streak {_post._auth_fail_streak}/3); "
                "will retry next cycle",
                file=sys.stderr,
            )
            return None
        _post._auth_fail_streak = 0
        print(f"[worker] API error (POST {url}): {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[worker] API error (POST {url}): {e}", file=sys.stderr)
        return None

def _get(url: str, timeout: float = 10) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[worker] API error (GET {url}): {e}", file=sys.stderr)
        return None


PENDING_FINALIZE_DIR = "pending_finalize"


def pending_finalize_dir(home: Path) -> Path:
    return Path(home) / PENDING_FINALIZE_DIR


def write_pending_finalize(home: Path, payload: dict) -> Path:
    """F-056: persist finalize payload before the HTTP attempt."""
    d = pending_finalize_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{payload['run_id']}.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(dest)
    return dest


def pending_finalize_is_ack(resp: dict | None) -> bool:
    """True if the master accepted finalize or the attempt is gone (do not retry)."""
    if not resp:
        return False
    if resp.get("ok"):
        return True
    err = str(resp.get("error") or "").lower()
    return any(
        s in err
        for s in (
            "already finalized",
            "not running",
            "run not owned",
            "finalize_token mismatch",
        )
    )


def post_finalize(
    master_url: str,
    payload: dict,
    *,
    post_fn=None,
) -> dict | None:
    fn = post_fn or _post
    return fn(f"{master_url.rstrip('/')}/api/worker/finalize", payload)


_flush_pending_last_err: float = 0.0  # monotonic time of last logged delivery failure


def flush_pending_finalize(
    home: Path,
    master_url: str,
    *,
    post_fn=None,
) -> int:
    """Deliver leftover pending-finalize files. Returns how many remain.

    E8: only log delivery failures once per 60s to avoid spamming stderr when
    the master is transiently down and the daemon retries every tick.
    """
    global _flush_pending_last_err
    d = pending_finalize_dir(home)
    if not d.is_dir():
        return 0
    remaining = 0
    failed_count = 0
    for p in sorted(d.glob("*.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            remaining += 1
            continue
        if pending_finalize_is_ack(post_finalize(master_url, payload, post_fn=post_fn)):
            p.unlink(missing_ok=True)
            _flush_pending_last_err = 0.0  # reset on success
        else:
            remaining += 1
            failed_count += 1
    if failed_count:
        now = time.monotonic()
        if now - _flush_pending_last_err >= 60.0:
            print(
                f"[worker] flush_pending_finalize: {failed_count} file(s) not acked (master unreachable?)",
                file=sys.stderr,
            )
            _flush_pending_last_err = now
    return remaining


def finalize_run(
    home: Path,
    master_url: str,
    run_id: int,
    worker_id: str,
    token: str | None = None,
    *,
    extra: dict | None = None,
    attempts: int = 5,
    post_fn=None,
    sleep_fn=None,
) -> bool:
    """F-056: write outbox, retry POST while `_post` returns None/non-ack.

    Returns True if the master acked (or the attempt is gone). On False the
    pending file remains for `flush_pending_finalize` after reconnect/restart.
    """
    payload: dict = {"run_id": run_id, "worker_id": worker_id, **(extra or {})}
    if token:
        payload["finalize_token"] = token
    write_pending_finalize(home, payload)
    sleeper = sleep_fn or time.sleep
    delay = 1.0
    for i in range(attempts):
        resp = post_finalize(master_url, payload, post_fn=post_fn)
        if pending_finalize_is_ack(resp):
            (pending_finalize_dir(home) / f"{run_id}.json").unlink(missing_ok=True)
            return True
        if i < attempts - 1:
            sleeper(delay)
            delay = min(delay * 2, 8.0)
    print(
        f"[worker] finalize not acked after {attempts} attempts; pending {run_id} kept",
        file=sys.stderr,
    )
    return False


def get_sys_info() -> dict:

    from taskqueue.resources import _detect_gpus

    ram = psutil.virtual_memory().total / (1024 ** 3)
    cores = psutil.cpu_count(logical=True) or 1

    gpus = _detect_gpus()

    # B4: enrich GPU list with per-device compute_cap and live free VRAM via nvidia-smi.
    cuda_version = ""
    try:
        _result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,compute_cap,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if _result.returncode == 0:
            gpu_details = []
            for _line in _result.stdout.strip().split("\n"):
                if not _line.strip():
                    continue
                _parts = [p.strip() for p in _line.split(",")]
                if len(_parts) >= 5:
                    gpu_details.append(
                        {
                            "name": _parts[0],
                            "vram_gib": round(float(_parts[1]) / 1024, 2),
                            "vram_free_gib": round(float(_parts[2]) / 1024, 2),
                            "compute_cap": _parts[3],
                            "cuda_version": _parts[4],
                        }
                    )
            if gpu_details:
                gpus = gpu_details
                if gpus[0].get("cuda_version"):
                    cuda_version = gpus[0]["cuda_version"]
    except Exception:
        pass

    vram = round(sum(g.get("vram_gib", 0.0) for g in gpus), 1)
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    import os as _os
    return {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "ram_gib": round(ram, 1),
        "cpu_cores": cores,
        "gpu_vram_gib": vram,
        # Per-device inventory so the master can first-fit against a single
        # card instead of the pooled total (a 2x12GiB worker != one 24GiB GPU).
        "gpus": gpus,
        "python_version": python_version,
        "cuda_version": cuda_version,
        "stable": _os.environ.get("TQ_WORKER_STABLE", "0") == "1",
    }


def get_live_load() -> dict:
    """R-008: host live load for worker heartbeat (leftover capacity on the master)."""
    ram = psutil.virtual_memory()
    cores = psutil.cpu_count(logical=True) or 1
    vram_used = 0.0
    vram_total = 0.0
    gpu_free_list: list[dict] = []
    try:
        from taskqueue.resources import _detect_gpus
        gpus = _detect_gpus()
        vram_total = round(sum(float(g.get("vram_gib", 0.0)) for g in gpus), 2)
        vram_used = round(sum(float(g.get("vram_used_gib", 0.0)) for g in gpus), 2)
    except Exception:
        gpus = []
    # B4: per-device free VRAM (best-effort nvidia-smi live read).
    try:
        _r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if _r.returncode == 0:
            for _line in _r.stdout.strip().split("\n"):
                _line = _line.strip()
                if _line:
                    try:
                        gpu_free_list.append({"vram_free_gib": round(float(_line) / 1024, 2)})
                    except ValueError:
                        pass
    except Exception:
        pass
    try:
        cpu_used = sum(psutil.cpu_percent(percpu=True)) / 100.0
    except Exception:
        cpu_used = 0.0
    result: dict = {
        "ram_used_gib": round(ram.used / (1024 ** 3), 2),
        "ram_total_gib": round(ram.total / (1024 ** 3), 2),
        "vram_used_gib": vram_used,
        "vram_total_gib": vram_total,
        "cpu_used": round(cpu_used, 2),
        "cpu_total_cores": cores,
    }
    if gpu_free_list:
        result["gpu_free"] = gpu_free_list
    return result


def _check_worker_upgrade(
    master_url: str,
    home: Path,
    *,
    pin_version: bool = False,
    active_run: bool = False,
) -> bool:
    """W7.5: Check if master has a newer wheel and self-upgrade if so.

    Returns True if an upgrade was applied (process re-execs and never returns).
    Returns False if nothing to do or skipped.
    pin_version=True or active_run=True suppresses the upgrade.
    """
    import hashlib
    import shutil

    if pin_version:
        return False

    info = _get(f"{master_url}/api/worker/version")
    if not info or not info.get("upgrade_url") or not info.get("sha256"):
        return False

    master_ver = info.get("version", "")
    master_sha = info.get("sha256", "")
    try:
        mv = tuple(int(x) for x in master_ver.split(".")[:3])
        wv = tuple(int(x) for x in _WORKER_VERSION.split(".")[:3])
    except Exception:
        return False

    if mv <= wv:
        return False  # worker is current

    if active_run:
        print(
            f"[worker] Upgrade available ({_WORKER_VERSION} → {master_ver}), "
            "will upgrade after current task completes.",
            file=sys.stderr,
        )
        return False

    print(f"[worker] Auto-upgrade: {_WORKER_VERSION} → {master_ver}", file=sys.stderr)

    # Download wheel streaming to disk (avoid holding 10–50 MB in RAM).
    wheel_url = master_url.rstrip("/") + info["upgrade_url"]
    req = urllib.request.Request(wheel_url, headers=_auth_headers(), method="GET")
    wheels_dir = home / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    prev_wheel = wheels_dir / "worker_prev.whl"
    new_wheel = wheels_dir / "worker_new.whl"
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=120) as resp, open(new_wheel, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
        actual_sha = h.hexdigest()
    except Exception as e:
        print(f"[worker] Upgrade download failed: {e}", file=sys.stderr)
        new_wheel.unlink(missing_ok=True)
        return False

    # Verify SHA-256.
    if actual_sha != master_sha:
        print(
            f"[worker] Upgrade aborted: SHA-256 mismatch (got {actual_sha[:16]}…, "
            f"expected {master_sha[:16]}…)",
            file=sys.stderr,
        )
        new_wheel.unlink(missing_ok=True)
        return False

    # Backup current wheel for rollback.
    cur_wheel = wheels_dir / "worker.whl"
    if cur_wheel.is_file():
        shutil.copy2(cur_wheel, prev_wheel)
    new_wheel.replace(cur_wheel)

    # Install into the running Python environment.
    install_ok = False
    for installer in (
        [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(cur_wheel)],
        ["uv", "pip", "install", "--reinstall", str(cur_wheel)],
    ):
        try:
            result = subprocess.run(installer, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                install_ok = True
                break
            print(f"[worker] installer {installer[0]} failed: {result.stderr.strip()}", file=sys.stderr)
        except Exception as e:
            print(f"[worker] installer error: {e}", file=sys.stderr)

    if not install_ok:
        # Rollback: restore previous wheel.
        if prev_wheel.is_file():
            print("[worker] Upgrade failed — rolling back to previous wheel.", file=sys.stderr)
            for installer in (
                [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(prev_wheel)],
                ["uv", "pip", "install", "--reinstall", str(prev_wheel)],
            ):
                try:
                    r = subprocess.run(installer, capture_output=True, text=True, timeout=120)
                    if r.returncode == 0:
                        break
                except Exception:
                    pass
        return False

    print(f"[worker] Upgrade to {master_ver} successful — restarting.", file=sys.stderr)
    _restart_process()
    return True  # only reached when _restart_process is replaced (tests)


def _restart_process() -> None:
    """Re-exec the worker on the freshly installed wheel (separate so tests can replace it)."""
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except OSError as exc:
        print(f"[worker] FATAL: execv failed after upgrade: {exc} — exiting.", file=sys.stderr)
        sys.exit(1)
    # os.execv replaces the process image; if it returns, treat as fatal.
    sys.exit(1)


def _load_worker_conf(home: Path) -> tuple[bool, str | None]:
    """(pin_worker_version, per-worker token) from home/worker.yaml (W7.5, C1)."""
    conf_p = home / "worker.yaml"
    if conf_p.is_file():
        try:
            import yaml as _yaml
            _wconf = _yaml.safe_load(conf_p.read_text(encoding="utf-8")) or {}
            return bool(_wconf.get("pin_worker_version", False)), (str(_wconf.get("worker_token") or "").strip() or None)
        except Exception:
            pass
    return False, None


def _registration_info(worker_id: str, worker_token: str | None) -> tuple[dict, str]:
    """Registration payload and a fresh session id. C1: credential hash; C3: per-process instance id."""
    import hashlib as _hashlib
    import socket as _socket

    sys_info = get_sys_info()
    sys_info["worker_id"] = worker_id
    sys_info["tags"] = []
    sys_info["protocol"] = PROTOCOL_VERSION
    session_id = str(uuid.uuid4())
    sys_info["session_id"] = session_id
    if worker_token:
        sys_info["credential_hash"] = _hashlib.sha256(worker_token.encode()).hexdigest()
    sys_info["worker_instance_id"] = f"{_socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    return sys_info, session_id


def _recover_journal(home: Path, worker_id: str) -> None:
    """After a crash/restart: queue a finalize for every run the journal shows unfinished."""
    from taskqueue.worker_journal import gc_journal, read_journal, scan_unfinished

    _unfinished = scan_unfinished(home)
    if _unfinished:
        print(f"[worker] Journal recovery: {len(_unfinished)} unfinished run(s)", file=sys.stderr)
        for _run_id, _state in _unfinished:
            _entries = read_journal(home, _run_id)
            if _state == "replay_finalize":
                _exit_e = next((e for e in _entries if e.get("t") == "exit"), {})
                _exit_code = _exit_e.get("code", -1)
                print(f"[worker] Journal: replaying finalize run {_run_id} exit={_exit_code}", file=sys.stderr)
                _pf_path = pending_finalize_dir(home) / f"{_run_id}.json"
                if not _pf_path.is_file():
                    write_pending_finalize(home, {
                        "run_id": _run_id,
                        "worker_id": worker_id,
                        "session_id": str(uuid.uuid4()),
                        "status": "failed" if _exit_code not in (0,) else "completed",
                        "exit_code": _exit_code,
                    })
                gc_journal(home, _run_id)
            else:
                print(f"[worker] Journal: run {_run_id} has no exit (crashed mid-run)", file=sys.stderr)
                _pf_path = pending_finalize_dir(home) / f"{_run_id}.json"
                if not _pf_path.is_file():
                    write_pending_finalize(home, {
                        "run_id": _run_id,
                        "worker_id": worker_id,
                        "session_id": str(uuid.uuid4()),
                        "status": "unknown",
                        "exit_code": -1,
                    })


def run_worker(  # noqa: C901
    home: Path,
    master_url: str,
    worker_id: str,
    *,
    auth_token: str | None = None,
) -> None:
    set_auth_token(auth_token)
    master_url = master_url.rstrip("/")

    _pin_version, _worker_token = _load_worker_conf(home)
    # C1: use per-worker token if available, else fall back to shared auth_token
    if _worker_token:
        set_auth_token(_worker_token)

    sys_info, session_id = _registration_info(worker_id, _worker_token)
    _worker_instance_id = sys_info["worker_instance_id"]
    slots_lock = threading.Lock()
    slots: dict[int, dict] = {}
    drain_flag: list[bool] = [False]

    def _slot_count() -> int:
        with slots_lock:
            return len(slots)

    def _slot_run_ids() -> list[int]:
        with slots_lock:
            return list(slots)

    def _stop_slot(sl: dict) -> None:
        """Flag a slot to stop and write the cooperative-suspend control file."""
        sl["stop_flag"][0] = True
        ctrl = sl["control_file"][0]
        if ctrl is not None:
            try:
                ctrl.parent.mkdir(parents=True, exist_ok=True)
                ctrl.write_text(json.dumps({"action": "suspend"}), encoding="utf-8")
            except Exception:
                pass

    def _apply_stop(run_ids) -> None:
        """Stop the slots for the given run ids (ignores malformed ids and unknown runs)."""
        for rid in run_ids:
            try:
                irid = int(rid)
            except (TypeError, ValueError):
                continue
            with slots_lock:
                sl = slots.get(irid)
            if sl is not None:
                _stop_slot(sl)

    def _finalize(run_id: int, token: "str | None" = None, **extra: object) -> None:
        # F-056: `_post` returns None on failure — must check the result, not
        # exceptions. Persist the payload first so a restart can flush it.
        extra_payload = dict(extra)
        extra_payload.setdefault("session_id", session_id)
        finalize_run(
            home,
            master_url,
            run_id,
            worker_id,
            token=token,
            extra=extra_payload,
        )

    _log_seqs: dict[int, int] = {}

    def _log_chunk(run_id: int, text: str) -> None:
        # Seeded from the clock so a restarted worker never reuses a seq the
        # master already accepted for this run.
        seq = _log_seqs.get(run_id, int(time.time() * 1000)) + 1
        _log_seqs[run_id] = seq
        resp = _post(f"{master_url}/api/worker/log-chunk", {
            "run_id": run_id,
            "worker_id": worker_id,
            "session_id": session_id,
            "seq": seq,
            "chunk_b64": base64.b64encode(text.encode("utf-8")).decode(),
        })
        # ds2 §1.7: honor stop piggybacked on log-chunk (not only heartbeat).
        stop_ids = resp.get("stop_run_ids") if resp else None
        if stop_ids:
            _apply_stop(stop_ids)

    from taskqueue.worker_slot import WorkerSlot

    slot_runner = WorkerSlot(
        home=home,
        master_url=master_url,
        worker_id=worker_id,
        session_id=session_id,
        finalize_fn=_finalize,
        log_chunk_fn=_log_chunk,
        slots_lock=slots_lock,
        slots=slots,
    )


    _recover_journal(home, worker_id)

    print(f"[worker] Starting worker '{worker_id}' session={session_id}...")
    print(f"[worker] System Info: OS={sys_info['os']}, Arch={sys_info['arch']}, RAM={sys_info['ram_gib']}G, Cores={sys_info['cpu_cores']}, VRAM={sys_info['gpu_vram_gib']}G")
    print(f"[worker] Master Coordinator: {master_url}")
    if _AUTH_TOKEN:
        print("[worker] Auth token: set")

    def heartbeat_loop():
        while True:
            try:
                _heartbeat_once()
            except Exception as _e:
                print(f"[worker] heartbeat_loop error: {_e}", file=sys.stderr)
                time.sleep(5)

    def _heartbeat_once():
        payload: dict = {
            "worker_id": worker_id,
            "session_id": session_id,
            "worker_instance_id": _worker_instance_id,
            "protocol": PROTOCOL_VERSION,
            "version": _WORKER_VERSION,
            "schema": _WORKER_SCHEMA,
            "load": get_live_load(),
        }
        run_ids = _slot_run_ids()
        if run_ids:
            payload["run_ids"] = run_ids
        resp = _post(f"{master_url}/api/worker/heartbeat", payload)
        if resp and resp.get("drain_and_stop"):
            print(
                f"[worker] master requested drain-and-stop (version mismatch): {resp.get('reason', '')}",
                file=sys.stderr,
            )
            drain_flag[0] = True
            with slots_lock:
                for sl in slots.values():
                    sl["stop_flag"][0] = True
        stop_ids = resp.get("stop_run_ids") if resp else None
        if stop_ids:
            _apply_stop(stop_ids)
        elif resp and resp.get("stop_requested"):
            with slots_lock:
                targets = list(slots.values()) if slots else []
            for target in targets:
                _stop_slot(target)
        if resp and resp.get("sync_requested"):
            # F-107: run sync in a background thread so the heartbeat is not blocked
            # during git pull + uv sync (which can take tens of seconds).
            import threading as _threading
            _t = _threading.Thread(target=_do_worker_sync, args=(home,), daemon=True, name="tq-worker-sync")
            _t.start()
        if resp and resp.get("file_sync_requested"):
            import threading as _threading
            _t = _threading.Thread(
                target=_do_worker_file_sync,
                args=(home, master_url),
                daemon=True,
                name="tq-worker-file-sync",
            )
            _t.start()
        # R-012: if heartbeats fail, stop children before the master reclaims the lease.
        from taskqueue.fencing import should_stop_for_lost_lease
        hb_ok = bool(resp and resp.get("ok"))
        if hb_ok:
            ttl = resp.get("lease_ttl_sec")
            if ttl is not None:
                try:
                    exp = time.time() + float(ttl)
                except (TypeError, ValueError):
                    exp = None
                else:
                    with slots_lock:
                        for sl in slots.values():
                            sl.setdefault("lease_expires_at", [None])[0] = exp
        now_wall = time.time()
        with slots_lock:
            for sl in list(slots.values()):
                expires = sl.get("lease_expires_at", [None])[0]
                if not should_stop_for_lost_lease(now_wall, expires, heartbeat_ok=hb_ok):
                    # ds2 §1.2: if the first heartbeat never succeeded (expires is None)
                    # and heartbeats keep failing past a grace window, stop the child
                    # before the master reclaims the lease and re-runs the task.
                    if expires is None and not hb_ok:
                        created = sl.get("created_at", [now_wall])[0]
                        if now_wall - created < 90:
                            continue
                    else:
                        continue
                sl["stop_flag"][0] = True
                proc = sl.get("proc", [None])[0]
                if proc is not None:
                    try:
                        terminate_tree(proc, background=True)
                    except Exception:
                        pass
                print(
                    "[worker] lease fence: master unreachable; stopping child before lease expiry",
                    file=sys.stderr,
                )
        from taskqueue.config import WORKER_HEARTBEAT_SEC
        time.sleep(WORKER_HEARTBEAT_SEC)

    h_thread = threading.Thread(target=heartbeat_loop, daemon=True)

    def _dispatch_command(cmd: dict) -> None:
        """G1: dispatch a single command dict received from /api/worker/commands."""
        kind = cmd.get("type")
        if kind == "stop_run_ids":
            stop_ids = cmd.get("run_ids") or []
            _apply_stop(stop_ids)
        elif kind == "drain":
            drain_flag[0] = True
            with slots_lock:
                for sl in slots.values():
                    sl["stop_flag"][0] = True
        elif kind == "sync":
            import threading as _threading
            _t = _threading.Thread(target=_do_worker_sync, args=(home,), daemon=True, name="tq-worker-sync")
            _t.start()
        elif kind == "file_sync":
            import threading as _threading
            _t = _threading.Thread(
                target=_do_worker_file_sync,
                args=(home, master_url),
                daemon=True,
                name="tq-worker-file-sync",
            )
            _t.start()

    def command_loop() -> None:
        """G1: long-poll /api/worker/commands for sub-second command delivery."""
        while True:
            try:
                resp = _get(
                    f"{master_url}/api/worker/commands"
                    f"?worker_id={worker_id}&session_id={session_id}&wait=25"
                )
                if resp and isinstance(resp.get("commands"), list):
                    for cmd in resp["commands"]:
                        _dispatch_command(cmd)
                elif resp is None:
                    time.sleep(2)  # master unreachable: do not hot-spin
            except Exception as _e:
                print(f"[worker] command_loop error: {_e}", file=sys.stderr)
                time.sleep(5)

    c_thread = threading.Thread(target=command_loop, daemon=True, name="tq-worker-cmd")

    last_register = 0.0
    _finalize_fail_streak = 0
    while True:
        # F-039: drain_and_stop means "finish current task then exit" — check after each task
        if drain_flag[0]:
            if _slot_count() > 0:
                time.sleep(1)
                continue
            print("[worker] drain-and-stop complete — exiting.", file=sys.stderr)
            return
        now = time.monotonic()
        if now - last_register > 60:
            projects = load_projects(home)
            sys_info["projects"] = list(projects.keys())
            reg = _post(f"{master_url}/api/worker/register", sys_info)
            if reg and reg.get("ok"):
                master_proto = reg.get("protocol")
                if master_proto is not None:
                    try:
                        mp = int(master_proto)
                        if mp < MIN_SUPPORTED_PROTOCOL:
                            print(
                                f"[worker] FATAL: master protocol v{mp} is too old "
                                f"(minimum supported: v{MIN_SUPPORTED_PROTOCOL}). Exiting.",
                                file=sys.stderr,
                            )
                            sys.exit(1)
                        if mp > MAX_SUPPORTED_PROTOCOL:
                            print(
                                f"[worker] WARNING: master protocol v{mp} is newer than "
                                f"this worker (max supported: v{MAX_SUPPORTED_PROTOCOL}). "
                                "Continuing, but some features may not work.",
                                file=sys.stderr,
                            )
                    except (TypeError, ValueError):
                        pass
                last_register = now
                # W7.5: check for available upgrade when idle
                if not _pin_version and _slot_count() == 0:
                    _check_worker_upgrade(
                        master_url, home,
                        pin_version=_pin_version,
                        active_run=_slot_count() > 0,
                    )
                if not h_thread.is_alive():
                    # W1.7: threads can't be re-started after they die; create a new one.
                    h_thread = threading.Thread(target=heartbeat_loop, daemon=True)
                    h_thread.start()
                if not c_thread.is_alive():
                    c_thread = threading.Thread(target=command_loop, daemon=True, name="tq-worker-cmd")
                    c_thread.start()
            else:
                err = (reg or {}).get("error") if reg else None
                print(f"[worker] Registration failed{': ' + err if err else ''}, retrying in 10s...", file=sys.stderr)
                time.sleep(10)
                continue

        # F-056: do not claim new work until leftover finalizes are acked.
        if flush_pending_finalize(home, master_url) > 0:
            _finalize_fail_streak += 1
            # ds2 §7.5: exponential backoff so a prolonged master outage doesn't spin at 5s.
            backoff = min(5 * (2 ** (_finalize_fail_streak - 1)), 300)
            print(
                f"[worker] pending finalize not acked; not claiming new work (retry in {backoff}s)",
                file=sys.stderr,
            )
            time.sleep(backoff)
            continue
        _finalize_fail_streak = 0

        if drain_flag[0]:
            time.sleep(1)
            continue

        wait_sec = max(0, int(WORK_LONGPOLL_MAX_SEC) - 5)
        task_resp = _get(
            f"{master_url}/api/worker/next-task?worker_id={worker_id}"
            f"&protocol={PROTOCOL_VERSION}&session_id={session_id}"
            f"&wait={wait_sec}",
            timeout=float(wait_sec) + 15.0,
        )
        if task_resp and task_resp.get("error") and not task_resp.get("task"):
            print(f"[worker] next-task error: {task_resp['error']}", file=sys.stderr)
            time.sleep(5)
            continue

        # H5: batch claiming — server returns {"tasks": [...]} for count > 1.
        if task_resp and isinstance(task_resp.get("tasks"), list):
            for single in task_resp["tasks"]:
                if not single.get("task"):
                    continue
                _run_id = int(single["run_id"])
                _slot = {"stop_flag": [False], "control_file": [None], "proc": [None], "lease_expires_at": [None], "created_at": [time.time()]}
                _ttl = single.get("lease_ttl_sec")
                if _ttl is not None:
                    try:
                        _slot["lease_expires_at"][0] = time.time() + float(_ttl)
                    except (TypeError, ValueError):
                        pass
                with slots_lock:
                    slots[_run_id] = _slot
                slot_runner.start(single, _slot, _run_id)
            continue

        if not task_resp or not task_resp.get("task"):
            if not task_resp:
                time.sleep(5)
            continue

        run_id = int(task_resp["run_id"])
        slot = {"stop_flag": [False], "control_file": [None], "proc": [None], "lease_expires_at": [None], "created_at": [time.time()]}
        ttl = task_resp.get("lease_ttl_sec")
        if ttl is not None:
            try:
                slot["lease_expires_at"][0] = time.time() + float(ttl)
            except (TypeError, ValueError):
                pass
        with slots_lock:
            slots[run_id] = slot

        slot_runner.start(task_resp, slot, run_id)


def _do_worker_file_sync(home: Path, master_url: str) -> None:
    """Allowlisted file pull from master (tq resync / file_sync_requested)."""
    print("[worker] file_sync_requested: pulling allowlisted project files...")
    try:
        projects = load_projects(home)
    except Exception as e:
        print(f"[worker] file-sync: failed to load projects: {e}", file=sys.stderr)
        return
    from taskqueue.project_sync import pull_from_master

    for name, proj in projects.items():
        root = proj.root if hasattr(proj, "root") else None
        if not root:
            continue
        try:
            result = pull_from_master(
                home=home,
                master_url=master_url,
                project=name,
                project_root=root,
                auth_headers=_auth_headers(),
                force=True,
            )
            if result.get("ok"):
                print(
                    f"[worker] file-sync {name}: "
                    f"fetched={result.get('fetched', 0)} skipped={result.get('skipped')}"
                )
            else:
                print(
                    f"[worker] file-sync {name}: {result.get('error', 'failed')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[worker] file-sync {name}: {e}", file=sys.stderr)


_SYNC_LOCK = threading.Lock()


def _do_worker_sync(home: Path) -> None:
    """W7.8: git pull + uv sync for every project configured on this worker."""
    if not _SYNC_LOCK.acquire(blocking=False):
        print("[worker] sync already running; ignoring duplicate request")
        return
    try:
        _do_worker_sync_locked(home)
    finally:
        _SYNC_LOCK.release()


def _do_worker_sync_locked(home: Path) -> None:
    print("[worker] sync_requested: updating all projects...")
    try:
        projects = load_projects(home)
    except Exception as e:
        print(f"[worker] sync: failed to load projects: {e}", file=sys.stderr)
        return
    for name, proj in projects.items():
        root = proj.root if hasattr(proj, "root") else None
        if not root or not root.is_dir():
            continue
        try:
            from taskqueue.win_console import hide_console_check_output_kwargs
            _nt = hide_console_check_output_kwargs()
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(root),
                capture_output=True,
                text=True,
                **_nt,
            )
            if result.returncode == 0:
                print(f"[worker] sync: {name} git pull OK — {result.stdout.strip() or 'already up to date'}")
            else:
                print(f"[worker] sync: {name} git pull failed: {result.stderr.strip()}", file=sys.stderr)
        except Exception as e:
            print(f"[worker] sync: {name} git pull error: {e}", file=sys.stderr)
            continue
        if (root / "pyproject.toml").is_file():
            try:
                uv = subprocess.run(
                    ["uv", "sync"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    **_nt,
                )
                if uv.returncode == 0:
                    print(f"[worker] sync: {name} uv sync OK")
                else:
                    err = (uv.stderr or uv.stdout or "").strip()
                    print(
                        f"[worker] sync: {name} uv sync failed (rc={uv.returncode}): {err}",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"[worker] sync: {name} uv sync error: {e}", file=sys.stderr)
    print("[worker] sync complete.")


def install_worker_service(home: Path, worker_id: str) -> None:
    """B4/E5: Install the worker as a platform service."""
    if sys.platform == "win32":
        _install_windows_service(home, worker_id)
    elif sys.platform.startswith("linux"):
        _install_systemd_service(home, worker_id)
    else:
        print(f"[install-service] Platform {sys.platform!r} not yet supported.")


def _install_windows_service(home: Path, worker_id: str) -> None:
    python_exe = sys.executable
    script = f'"{python_exe}" -m taskqueue.cli worker run --home "{home}" --id "{worker_id}"'
    task_name = f"tq-worker-{worker_id}"
    cmd = ["schtasks", "/create", "/tn", task_name, "/tr", script,
           "/sc", "onstart", "/ru", "SYSTEM", "/f"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[install-service] Task Scheduler task {task_name!r} created.")
        else:
            print(f"[install-service] Failed: {result.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[install-service] Error: {e}", file=sys.stderr)


def _install_systemd_service(home: Path, worker_id: str) -> None:
    python_exe = sys.executable
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_name = f"tq-worker-{worker_id}.service"
    unit_path = unit_dir / unit_name
    unit_path.write_text(
        f"[Unit]\nDescription=taskqueue worker {worker_id}\nAfter=network.target\n\n"
        f"[Service]\nType=simple\n"
        f"ExecStart={python_exe} -m taskqueue.cli worker run --home {home} --id {worker_id}\n"
        f"Restart=always\nRestartSec=10\nEnvironment=PYTHONUNBUFFERED=1\n\n"
        f"[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    print(f"[install-service] Wrote {unit_path}")
    print(f"[install-service] Enable with: systemctl --user enable --now {unit_name}")


def uninstall_worker_service(home: Path, worker_id: str) -> int:
    if sys.platform == "win32":
        task_name = f"tq-worker-{worker_id}"
        res = subprocess.run(["schtasks", "/delete", "/tn", task_name, "/f"], capture_output=True, text=True)
        if res.returncode != 0:
            # e.g. task missing, or created as SYSTEM and this shell is not elevated
            detail = (res.stderr or res.stdout or "").strip()
            print(f"[uninstall-service] Could not remove task {task_name!r}: {detail}", file=sys.stderr)
            return 1
        print(f"[uninstall-service] Removed task {task_name!r}")
    elif sys.platform.startswith("linux"):
        unit_path = Path.home() / ".config" / "systemd" / "user" / f"tq-worker-{worker_id}.service"
        if unit_path.exists():
            unit_path.unlink()
            print(f"[uninstall-service] Removed {unit_path}")
        else:
            print(f"[uninstall-service] Unit not found: {unit_path}")
            return 1
    return 0


def setup_worker(
    home: Path,
    master_url: str,
    worker_id: str | None = None,
    *,
    auth_token: str | None = None,
) -> None:
    set_auth_token(auth_token)
    master_url = master_url.rstrip("/")
    print(f"[setup] Querying project configuration from master: {master_url}...")

    try:
        req = urllib.request.Request(
            f"{master_url}/api/worker/projects",
            headers=_auth_headers(),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            projs = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[setup] Failed to fetch project list: {e}", file=sys.stderr)
        return

    if not projs:
        print("[setup] No projects registered on master. Nothing to set up.")
        return

    print(f"[setup] Found {len(projs)} registered projects:")
    for name, p in projs.items():
        print(f"  - {name} (git: {p['git_url'] or 'none'})")

    from taskqueue.config import registry_path
    reg_p = registry_path(home)
    reg_p.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if reg_p.exists():
        import yaml
        try:
            raw = yaml.safe_load(reg_p.read_text(encoding="utf-8")) or {}
            existing = raw.get("projects") or {}
        except Exception:
            pass

    new_projects = {}
    for name, p in projs.items():
        git_url = p.get("git_url")
        if "test_tq_worker" in str(home) or home.name != ".taskqueue":
            default_root = home / "projects" / name
        else:
            default_root = Path.home() / "projects" / name
            if os.name == "nt" and Path("D:\\").is_dir():
                default_root = Path("D:\\") / "antigravity" / name

        if name in existing:
            local_root = Path(existing[name].get("root", str(default_root)))
            print(f"[setup] Project '{name}' already configured locally at: {local_root}")
        else:
            local_root = default_root
            print(f"[setup] Setting up project '{name}' at: {local_root}")

        local_root.parent.mkdir(parents=True, exist_ok=True)

        if git_url and not local_root.is_dir():
            print(f"[setup] Cloning '{name}' from {git_url}...")
            try:
                from taskqueue.win_console import hide_console_check_output_kwargs
                _nt = hide_console_check_output_kwargs()
                subprocess.run(["git", "clone", git_url, str(local_root)], check=True, **_nt)
            except Exception as e:
                print(f"[setup] Failed to clone '{name}': {e}", file=sys.stderr)

        if local_root.is_dir():
            if (local_root / "pyproject.toml").is_file():
                print(f"[setup] Running 'uv sync' in {local_root}...")
                try:
                    from taskqueue.win_console import hide_console_check_output_kwargs
                    _nt = hide_console_check_output_kwargs()
                    subprocess.run(["uv", "sync"], cwd=str(local_root), **_nt)
                except Exception as e:
                    print(f"[setup] Failed to run 'uv sync': {e}", file=sys.stderr)

        new_projects[name] = {
            **(existing.get(name) or {}),
            "root": str(local_root),
            "git_url": git_url,
        }

    import yaml
    # Keep projects registered only on this worker; the master list adds/updates, never prunes.
    merged = {**{k: v for k, v in existing.items() if k not in new_projects}, **new_projects}
    reg_p.write_text(yaml.dump({"projects": merged}, default_flow_style=False), encoding="utf-8")
    print(f"[setup] Successfully wrote projects configuration to: {reg_p}")

    import secrets as _secrets
    # C1: preserve existing worker_token if present; generate one on first setup
    worker_yaml_p = home / "worker.yaml"
    existing_token: str | None = None
    if worker_yaml_p.is_file():
        try:
            import yaml as _yaml2
            _existing_conf = _yaml2.safe_load(worker_yaml_p.read_text(encoding="utf-8")) or {}
            existing_token = str(_existing_conf.get("worker_token") or "").strip() or None
        except Exception:
            pass
    worker_token = existing_token or _secrets.token_hex(32)
    worker_conf = {
        "master_url": master_url,
        "worker_id": worker_id or platform.node(),
        "worker_token": worker_token,
    }
    worker_yaml_p.write_text(yaml.dump(worker_conf, default_flow_style=False), encoding="utf-8")
    print(f"[setup] Saved worker configuration to: {worker_yaml_p}")
