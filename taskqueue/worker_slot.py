"""Per-slot task execution for remote workers."""

from __future__ import annotations
from taskqueue.reap import terminate_tree
from taskqueue.jsonutil import json_list

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil




def child_stdio_kwargs() -> dict[str, Any]:
    """Popen stdio for task processes: merged stdout/stderr, decoded as UTF-8 (never the
    OS code page, which crashes the reader on bytes like 0x81), undecodable bytes replaced."""
    return {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }

def verify_bundle_sha(path: Path, expected: str) -> None:
    """Raise ValueError when a downloaded bundle's SHA-256 differs from its content-addressed id.

    Bundle ids are the archive's SHA-256; any other identifier shape is not checked.
    """
    import hashlib
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
        return
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise ValueError(f"bundle sha256 mismatch (expected {expected[:12]}, got {h.hexdigest()[:12]})")


class _LogBatcher:
    """Accumulate log chunks for ~2 s then POST them as a single request.

    Reduces HTTP request count by ~10x for tasks that emit frequent lines.
    """

    def __init__(self, flush_fn: Callable[[str], None], interval_sec: float = 2.0) -> None:
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._flush_fn = flush_fn
        self._interval = interval_sec
        self._timer: threading.Timer | None = None

    def add(self, chunk: str) -> None:
        with self._lock:
            self._buf.append(chunk)
            if self._timer is None:
                self._timer = threading.Timer(self._interval, self._fire)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            buf = self._buf[:]
            self._buf.clear()
        if buf:
            self._flush_fn("".join(buf))

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            buf = self._buf[:]
            self._buf.clear()
        if buf:
            self._flush_fn("".join(buf))

from taskqueue.config import load_projects
from taskqueue.worker import _auth_headers


def _transfer_headers(finalize_token: Any = None, extra: dict | None = None) -> dict[str, str]:
    """Auth headers plus X-TQ-Finalize-Token (F-121 — prefer header over query)."""
    h = _auth_headers(extra or {})
    if finalize_token:
        h["X-TQ-Finalize-Token"] = str(finalize_token)
    return h
from taskqueue.worker_journal import append_entry, gc_journal


# Task env may not redirect the worker's own control variables.
_RESERVED_ENV = frozenset({"TQ_HOME", "TQ_PROJECT", "TQ_TASK_ID", "TQ_CONTROL_FILE", "TQ_WORKSPACE", "TQ_PACKAGE_HASH"})


class WorkerSlot:
    """Owns execution of one claimed remote task in a daemon thread."""

    def __init__(
        self,
        *,
        home: Path,
        master_url: str,
        worker_id: str,
        session_id: str,
        finalize_fn: Callable[..., None],
        log_chunk_fn: Callable[[int, str], None],
        slots_lock: threading.Lock,
        slots: dict,
    ) -> None:
        self.home = home
        self.master_url = master_url
        self.worker_id = worker_id
        self.session_id = session_id
        self._finalize = finalize_fn
        self._log_chunk = log_chunk_fn
        self.slots_lock = slots_lock
        self.slots = slots

    def start(self, resp: dict, sl: dict, rid: int) -> None:
        """Start a daemon thread running self.run."""
        threading.Thread(
            target=self.run,
            args=(resp, sl, rid),
            daemon=True,
            name=f"tq-slot-{rid}",
        ).start()

    def _pull_project_sync(self, proj_name: str, project: Any) -> None:
        """Lazy allowlist sync of the project tree from the master (fingerprint short-circuit)."""
        home = self.home
        master_url = self.master_url
        try:
            from taskqueue.project_sync import pull_from_master
            sync_result = pull_from_master(
                home=home,
                master_url=master_url,
                project=proj_name,
                project_root=project.root,
                auth_headers=_auth_headers(),
            )
            if sync_result.get("ok"):
                if sync_result.get("skipped"):
                    print(f"[worker] sync {proj_name}: up to date")
                else:
                    print(
                        f"[worker] sync {proj_name}: fetched {sync_result.get('fetched', 0)} file(s)"
                    )
            elif sync_result.get("error"):
                print(
                    f"[worker] sync {proj_name}: {sync_result.get('error')}",
                    file=sys.stderr,
                )
        except Exception as sync_exc:
            print(f"[worker] sync {proj_name} failed: {sync_exc}", file=sys.stderr)

    def _materialize_workspace(self, resp: dict, task: dict, proj_name: str, project: Any, run_id: int) -> tuple[Path, Any]:
        """Per-attempt code workspace (git_patch / bundle), else project.root. Raises on failure."""
        home = self.home
        master_url = self.master_url
        bundle_hash = task.get("bundle_hash")
        work_root = project.root
        from taskqueue.config import state_home as _state_home
        from taskqueue.provision import (
            PROVIDER_BUNDLE,
            PROVIDER_GIT_PATCH,
            code_package_from_dict,
            materialize_bundle_package,
            materialize_git_package,
            overlay_path,
            package_from_bundle,
            prepare_attempt_workspace,
        )
        pkg = code_package_from_dict(resp.get("code_package"))
        if pkg and pkg.provider == PROVIDER_GIT_PATCH:
            overlay = None
            if pkg.patch_hash:
                dest = overlay_path(home, pkg.project or proj_name, pkg.patch_hash)
                if not dest.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    overlay_url = (
                        f"{master_url}/api/package/"
                        f"{pkg.project or proj_name}/{pkg.patch_hash}"
                    )
                    req = urllib.request.Request(overlay_url, headers=_auth_headers())
                    with urllib.request.urlopen(req, timeout=120) as oresp:
                        dest.write_bytes(oresp.read())
                overlay = dest
            cache = materialize_git_package(
                home=home,
                package=pkg,
                git_root=project.root,
                fetch_overlay=overlay,
            )
            work_root = prepare_attempt_workspace(home, run_id, cache)
            print(
                f"[worker] git_patch workspace {pkg.package_hash[:12]} → {work_root}"
            )
        elif bundle_hash or (pkg and pkg.provider == PROVIDER_BUNDLE):
            bh = (pkg.bundle_hash if pkg else None) or bundle_hash
            pkg = pkg or package_from_bundle(proj_name, str(bh))
            import shutil
            import tarfile
            import tempfile

            from taskqueue.safetar import safe_extract

            cache_parent = _state_home() / "bundles" / proj_name
            cache_parent.mkdir(parents=True, exist_ok=True)
            local_bundle_dir = cache_parent / str(bh)
            cached = local_bundle_dir.is_dir() and any(local_bundle_dir.iterdir())
            if not cached:
                if local_bundle_dir.exists():
                    shutil.rmtree(local_bundle_dir, ignore_errors=True)
                print(f"[worker] Downloading bundle '{bh}' from master...")
                with tempfile.TemporaryDirectory(prefix="tq-bundle-") as tmp:
                    tmp_path = Path(tmp)
                    tar_path = tmp_path / "bundle.tar.gz"
                    extract_dir = tmp_path / "out"
                    extract_dir.mkdir()
                    bundle_url = f"{master_url}/api/bundle/{proj_name}/{bh}"
                    req = urllib.request.Request(bundle_url, headers=_auth_headers())
                    with urllib.request.urlopen(req, timeout=120) as bresp, open(
                        tar_path, "wb"
                    ) as out:
                        out.write(bresp.read())
                    verify_bundle_sha(tar_path, str(bh))
                    with tarfile.open(tar_path, "r:gz") as tar:
                        safe_extract(tar, extract_dir)
                    shutil.move(str(extract_dir), str(local_bundle_dir))
            cache = materialize_bundle_package(
                home, pkg, extracted_bundle_dir=local_bundle_dir
            )
            work_root = prepare_attempt_workspace(home, run_id, cache)
            print(
                f"[worker] bundle workspace {pkg.package_hash[:12]} → {work_root} "
                "(not unpacked onto project.root)"
            )
        return work_root, pkg

    def _push_transfers(self, task: dict, work_root: Path, proj_name: str, run_id: int, finalize_token: Any) -> list[Any]:
        """W7.6 pre-task transfer (direction=push: master to worker). Returns all transfer specs."""
        master_url = self.master_url
        transfer_specs: list[Any] = []
        try:
            transfer_specs = json_list(task.get("transfer")) or []
        except Exception:
            pass
        for spec in transfer_specs:
            if spec.get("direction", "push") != "push":
                continue
            src = str(spec.get("src", ""))
            dst = str(spec.get("dst", src))
            if not src:
                continue
            try:
                from urllib.parse import quote, urlencode
                q = urlencode({"run_id": str(run_id)})
                url = (
                    f"{master_url}/api/transfer/get/"
                    f"{quote(proj_name, safe='')}/{quote(src, safe='/')}?{q}"
                )
                req = urllib.request.Request(url, headers=_transfer_headers(finalize_token))
                with urllib.request.urlopen(req, timeout=60) as tresp:
                    data = tresp.read()
                dest_path = work_root / dst
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(data)
                print(f"[worker] transfer push: {src} → {dest_path} ({len(data)} bytes)")
            except Exception as te:
                print(f"[worker] transfer push failed for {src}: {te}", file=sys.stderr)
                raise RuntimeError(f"transfer push failed for {src}: {te}") from te
        return transfer_specs

    def _fetch_data_inputs(self, task: dict, work_root: Path, run_id: int, finalize_token: Any) -> None:
        """D1: content-addressed data.inputs into the workspace (hardlinked from the blob cache)."""
        home = self.home
        master_url = self.master_url
        _data_raw = task.get("data")
        data_block = None
        if _data_raw:
            try:
                data_block = json.loads(_data_raw) if isinstance(_data_raw, str) else _data_raw
            except Exception:
                data_block = None

        if data_block and isinstance(data_block.get("inputs"), list):
            import shutil as _shutil
            run_cache = Path(home) / "cache" / "data"
            run_cache.mkdir(parents=True, exist_ok=True)

            for inp in data_block["inputs"]:
                if not isinstance(inp, dict):
                    continue
                provider = inp.get("provider", "fetch")
                mount = inp.get("mount", "")

                if provider == "fetch":
                    from urllib.parse import quote as _quote
                    mount_dest = work_root / mount if mount else work_root

                    manifest_url = f"{master_url}/api/data/manifest/{run_id}?run_id={run_id}"
                    try:
                        mreq = urllib.request.Request(
                            manifest_url, headers=_transfer_headers(finalize_token)
                        )
                        with urllib.request.urlopen(mreq, timeout=30) as mresp:
                            manifest_data = json.loads(mresp.read().decode("utf-8"))
                    except Exception as me:
                        print(f"[worker] data manifest fetch failed: {me}", file=sys.stderr)
                        continue

                    for mf in manifest_data.get("manifests") or []:
                        if mf.get("mount") != mount:
                            continue
                        for entry in mf.get("files") or []:
                            sha256 = entry["sha256"]
                            rel_path = entry["path"]
                            dest_file = mount_dest / rel_path

                            cached = run_cache / sha256
                            if not cached.is_file():
                                blob_url = f"{master_url}/api/data/blob/{sha256}?run_id={run_id}"
                                try:
                                    breq = urllib.request.Request(
                                        blob_url, headers=_transfer_headers(finalize_token)
                                    )
                                    with urllib.request.urlopen(breq, timeout=120) as bresp:
                                        data_bytes = bresp.read()
                                    cached.parent.mkdir(parents=True, exist_ok=True)
                                    btmp = cached.with_suffix(".tmp")
                                    btmp.write_bytes(data_bytes)
                                    btmp.replace(cached)
                                except Exception as be:
                                    print(f"[worker] data blob fetch failed {sha256[:8]}: {be}", file=sys.stderr)
                                    continue

                            dest_file.parent.mkdir(parents=True, exist_ok=True)
                            if dest_file.is_file():
                                # A file left from an earlier input version is stale: replace it.
                                try:
                                    _same = os.path.samefile(str(cached), str(dest_file))
                                except OSError:
                                    _same = False
                                if not _same:
                                    try:
                                        dest_file.unlink()
                                    except OSError:
                                        pass
                            if not dest_file.is_file():
                                try:
                                    os.link(str(cached), str(dest_file))
                                except (OSError, NotImplementedError):
                                    _shutil.copy2(str(cached), str(dest_file))

    def _sync_shared_data(self, proj_name: str, project: Any) -> None:
        """Preflight: refresh the shared data/ files on project.root from the master manifest."""
        master_url = self.master_url
        print(f"[worker] Synchronizing input data for project '{proj_name}'...")
        try:
            manifest_url = f"{master_url}/api/data/manifest/{proj_name}"
            manifest_req = urllib.request.Request(manifest_url, headers=_auth_headers())
            with urllib.request.urlopen(manifest_req, timeout=15) as mresp:
                master_manifest = json.loads(mresp.read().decode("utf-8"))

            for f_info in master_manifest.get("files") or []:
                f_path = f_info["path"]
                f_size = f_info["size"]
                f_mtime = f_info["mtime"]

                local_f = project.resolve(f_path)
                need_download = False
                if not local_f.is_file():
                    need_download = True
                else:
                    stat = local_f.stat()
                    if stat.st_size != f_size or stat.st_mtime < f_mtime:
                        need_download = True

                if need_download:
                    print(f"[worker] Downloading data file: {f_path}...")
                    local_f.parent.mkdir(parents=True, exist_ok=True)
                    quoted_path = urllib.parse.quote(f_path)
                    file_url = f"{master_url}/api/data/file/{proj_name}/{quoted_path}"
                    file_req = urllib.request.Request(file_url, headers=_auth_headers())
                    with urllib.request.urlopen(file_req, timeout=120) as fresp, open(
                        local_f, "wb"
                    ) as out:
                        out.write(fresp.read())
                    os.utime(local_f, (time.time(), f_mtime))
        except Exception as e:
            print(f"[worker] Data sync warning (skipping file sync): {e}", file=sys.stderr)

    def _pull_transfers(self, transfer_specs: list[Any], work_root: Path, project: Any, proj_name: str, run_id: int, finalize_token: Any) -> None:
        """W7.6 post-task transfer (direction=pull: worker to master)."""
        master_url = self.master_url
        for spec in transfer_specs:
            if spec.get("direction") != "pull":
                continue
            src = str(spec.get("src", ""))
            dst = str(spec.get("dst", src))
            if not src:
                continue
            try:
                src_path = work_root / src
                if not src_path.is_file():
                    src_path = project.root / src
                if not src_path.is_file():
                    print(f"[worker] transfer pull: {src} not found, skipping", file=sys.stderr)
                    continue
                data = src_path.read_bytes()
                from urllib.parse import quote, urlencode
                q = urlencode({"run_id": str(run_id)})
                upload_req = urllib.request.Request(
                    f"{master_url}/api/transfer/put/"
                    f"{quote(proj_name, safe='')}/{quote(dst, safe='/')}?{q}",
                    data=data,
                    headers=_transfer_headers(
                        finalize_token,
                        {"Content-Type": "application/octet-stream",
                         "Content-Length": str(len(data))},
                    ),
                    method="POST",
                )
                with urllib.request.urlopen(upload_req, timeout=60) as uresp:
                    uresp.read()
                print(f"[worker] transfer pull: {src} → master:{dst} ({len(data)} bytes)")
            except Exception as te:
                print(f"[worker] transfer pull failed for {src}: {te}", file=sys.stderr)

    def _upload_outputs(self, work_root: Path, project: Any, proj_name: str, task_start_time: float) -> None:
        """Push reports/ and experiments/ledger files written during the run to the master."""
        master_url = self.master_url
        print("[worker] Uploading generated metrics and logs back to coordinator...")
        try:
            # Prefer per-attempt workspace (git_patch/bundle); fall back to project.root.
            roots = []
            if work_root.is_dir():
                roots.append(work_root)
            if project.root not in roots:
                roots.append(project.root)
            for base in roots:
                output_dirs = [base / "reports", base / "experiments" / "ledger"]
                for o_dir in output_dirs:
                    if not o_dir.is_dir():
                        continue
                    for dirpath, _, filenames in os.walk(o_dir):
                        for f in filenames:
                            abs_p = Path(dirpath) / f
                            try:
                                mtime = abs_p.stat().st_mtime
                                if mtime >= task_start_time:
                                    try:
                                        rel_p = abs_p.relative_to(project.root)
                                    except ValueError:
                                        rel_p = abs_p.relative_to(base)
                                    rel_str = str(rel_p).replace("\\", "/")
                                    print(f"[worker] Pushing output file: {rel_str}...")
                                    data_bytes = abs_p.read_bytes()
                                    upload_req = urllib.request.Request(
                                        f"{master_url}/api/data/upload/{proj_name}",
                                        data=data_bytes,
                                        headers=_auth_headers({
                                            "Content-Type": "application/octet-stream",
                                            "X-File-Path": rel_str,
                                        }),
                                        method="POST"
                                    )
                                    with urllib.request.urlopen(upload_req, timeout=15) as upload_resp:
                                        json.loads(upload_resp.read().decode("utf-8"))
                            except Exception as e:
                                print(f"[worker] Output upload error for {f}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[worker] Output sync warning: {e}", file=sys.stderr)

    def run(self, resp: dict, sl: dict, rid: int) -> None:  # noqa: C901
        """Execute a claimed task until finalize (former _run_slot body)."""
        home = self.home
        _finalize = self._finalize
        _log_chunk = self._log_chunk
        slots_lock = self.slots_lock
        slots = self.slots

        stop_flag = sl["stop_flag"]
        control_file = sl["control_file"]
        run_id = rid
        task = resp["task"]
        finalize_token = resp.get("finalize_token")
        # B1: 2-second accumulation batcher — reduces log-chunk HTTP requests ~10x.
        batcher = _LogBatcher(lambda text: _log_chunk(run_id, text))
        proj_name = task["project"]
        task_id = task["task_id"]
        proc: subprocess.Popen | None = None
        try:
            print(f"[worker] Claimed task: {proj_name}/{task_id} (run_id={run_id})")
            try:
                append_entry(home, run_id, {"t": "claim", "run_id": run_id, "task_id": task_id, "session": self.session_id})
            except Exception:
                pass

            projects = load_projects(home)
            project = projects.get(proj_name)
            if not project:
                err_msg = f"Project '{proj_name}' not configured in worker's projects.yaml"
                print(f"[worker] {err_msg}", file=sys.stderr)
                _finalize(run_id, token=finalize_token, status="failed", exit_code=-1)
                return

            self._pull_project_sync(proj_name, project)

            try:
                work_root, pkg = self._materialize_workspace(resp, task, proj_name, project, run_id)
            except Exception as e:
                err_msg = f"Failed to materialize code workspace: {e}"
                print(f"[worker] {err_msg}", file=sys.stderr)
                _finalize(run_id, token=finalize_token, status="failed", exit_code=-1)
                return

            # W7.6: pre-task transfer (direction=push: master->worker)
            transfer_specs = self._push_transfers(task, work_root, proj_name, run_id, finalize_token)

            # D1: process data.inputs (content-addressed fetch)
            self._fetch_data_inputs(task, work_root, run_id, finalize_token)

            from taskqueue.platform_utils import resolve_command, pythonw_if_available
            command_path = resolve_command(task["command"], project.root)
            command_path = pythonw_if_available(command_path)

            self._sync_shared_data(proj_name, project)

            req_paths = []
            for p in task["requires_paths"]:
                rel = Path(str(p))
                ws_p = work_root / rel if not rel.is_absolute() else rel
                root_p = project.resolve(p)
                req_paths.append(ws_p if ws_p.exists() else root_p)
            missing = [str(p) for p in req_paths if not p.exists()]
            if missing:
                err_msg = f"Missing required paths on worker: {', '.join(missing)}"
                print(f"[worker] {err_msg}", file=sys.stderr)
                _log_chunk(run_id, f"Preflight error: {err_msg}\n")
                _finalize(run_id, token=finalize_token, status="failed", exit_code=-1)
                return

            use_resume = bool(resp.get("use_resume"))
            argv_tail = (
                task.get("resume_args")
                if use_resume and task.get("resume_args")
                else task.get("args", [])
            )
            argv = [str(command_path), *argv_tail]

            print(f"[worker] Executing: {' '.join(argv)}")
            task_start_time = time.time()
            wall_cap = task.get("wall_clock_cap_sec")
            try:
                wall_cap_sec = float(wall_cap) if wall_cap else 0.0
            except (TypeError, ValueError):
                wall_cap_sec = 0.0

            from taskqueue.config import control_path, controls_dir
            ctrl = control_path(home, proj_name, task_id)
            controls_dir(home).mkdir(parents=True, exist_ok=True)
            if not ctrl.exists():
                ctrl.write_text("{}", encoding="utf-8")
            control_file[0] = ctrl

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["TQ_HOME"] = str(home)
            env["TQ_PROJECT"] = proj_name
            env["TQ_TASK_ID"] = task_id
            env["TQ_CONTROL_FILE"] = str(ctrl)
            env["TQ_WORKSPACE"] = str(work_root)
            env["TQ_PACKAGE_HASH"] = (pkg.package_hash if pkg else "") or ""
            # F-045: apply task-level env vars (includes TQ_DRY_RUN from dry_run: true)
            try:
                _task_env_raw = task.get("env")
                if _task_env_raw:
                    _task_env = json.loads(_task_env_raw) if isinstance(_task_env_raw, str) else _task_env_raw
                    if isinstance(_task_env, dict):
                        env.update({str(k): str(v) for k, v in _task_env.items() if str(k) not in _RESERVED_ENV})
            except (TypeError, ValueError):
                pass
            from taskqueue.win_console import (
                apply_hide_console_popen_kwargs,
                inject_python_hide_console,
            )

            env = inject_python_hide_console(env)
            try:
                popen_kwargs = {
                    "args": argv,
                    "cwd": str(work_root),
                    **child_stdio_kwargs(),
                    "env": env,
                }
                if os.name == "nt":
                    popen_kwargs = apply_hide_console_popen_kwargs(popen_kwargs)
                proc = subprocess.Popen(**popen_kwargs)
                sl["proc"][0] = proc
                try:
                    append_entry(home, run_id, {"t": "start", "pid": proc.pid, "started_at": time.time()})
                except Exception:
                    pass
            except Exception as e:
                err_msg = f"Failed to spawn process: {e}"
                print(f"[worker] {err_msg}", file=sys.stderr)
                _log_chunk(run_id, f"Execution error: {err_msg}\n")
                _finalize(run_id, token=finalize_token, status="failed", exit_code=-1)
                return

            log_buffer: list[str] = []
            log_buffer_bytes = 0
            last_flush = time.monotonic()
            peak_ram = 0.0
            peak_vram = 0.0
            pids: list[int] = []
            stop_hard = [False]
            from taskqueue.resources import (
                attributed_gpu_pids,
                attributed_vram_gib,
                parse_json_list,
                parse_json_obj,
                probe_gpu_compute_apps,
                probe_gpu_memory_used,
                sidecar_name_hints,
                task_requires_gpu,
            )
            _res = parse_json_obj(task.get("resources"))
            _gpu_hints = sidecar_name_hints(_res, parse_json_list(task.get("requires_services")))
            _need_gpu = task_requires_gpu(_res) or bool(_gpu_hints)

            def monitor_and_cap_watchdog() -> None:
                """Merged monitor_resources + cap_and_stop_watchdog (§4.12)."""
                nonlocal peak_ram, peak_vram, pids
                # nvidia-smi is expensive — only call it every 5 loop
                # iterations (every ~5 s) while psutil RAM runs every 1 s.
                _NVIDIA_INTERVAL = 5
                _nvidia_tick = 0
                _children: list = []
                _children_refresh = 0.0
                stop_seen_at = 0.0
                parent = None
                try:
                    parent = psutil.Process(proc.pid)
                except Exception:
                    parent = None
                while proc.poll() is None:
                    if parent is not None:
                        try:
                            now = time.monotonic()
                            if now - _children_refresh >= 5.0:
                                try:
                                    _children = parent.children(recursive=True)
                                except Exception:
                                    _children = []
                                _children_refresh = now
                            ram_val = 0.0
                            try:
                                ram_val += parent.memory_info().rss
                            except Exception:
                                pass
                            for c in _children:
                                try:
                                    ram_val += c.memory_info().rss
                                except Exception:
                                    pass
                            peak_ram = max(peak_ram, ram_val / (1024 ** 3))
                            pids_current = [parent.pid] + [c.pid for c in _children]
                            pids = pids_current

                            _nvidia_tick += 1
                            if _need_gpu and _nvidia_tick >= _NVIDIA_INTERVAL:
                                _nvidia_tick = 0
                                probe = probe_gpu_compute_apps(timeout=2.0)
                                if probe.ok:
                                    device_used = None
                                    attributed = attributed_gpu_pids(
                                        probe.processes, pids_current, _gpu_hints
                                    )
                                    if attributed and not any(
                                        float(p.get("vram_gib") or 0) > 0 for p in attributed
                                    ):
                                        device_used = probe_gpu_memory_used(timeout=2.0)
                                    vram_gib = attributed_vram_gib(
                                        probe.processes,
                                        pids_current,
                                        _gpu_hints,
                                        device_used_gib=device_used,
                                    )
                                    peak_vram = max(peak_vram, vram_gib)
                        except Exception:
                            pass

                    if wall_cap_sec > 0 and (time.time() - task_start_time) >= wall_cap_sec:
                        print(
                            f"[worker] wall_clock_cap_sec={wall_cap_sec} exceeded; terminating",
                            flush=True,
                        )
                        stop_hard[0] = True
                        try:
                            terminate_tree(proc, background=True)
                        except Exception:
                            pass
                        break
                    if stop_flag[0]:
                        if stop_seen_at == 0.0:
                            stop_seen_at = time.monotonic()
                        elif time.monotonic() - stop_seen_at > 60.0:
                            print(
                                "[worker] stop_requested grace elapsed; terminating",
                                flush=True,
                            )
                            stop_hard[0] = True  # killed on request: interrupted, not failed
                            try:
                                terminate_tree(proc, background=True)
                            except Exception:
                                pass
                            break
                    time.sleep(1)

            mon_t = threading.Thread(target=monitor_and_cap_watchdog, daemon=True)
            mon_t.start()

            def flush_logs() -> None:
                nonlocal last_flush, log_buffer_bytes
                if not log_buffer:
                    return
                text = "".join(log_buffer)
                log_buffer.clear()
                log_buffer_bytes = 0
                batcher.add(text)
                last_flush = time.monotonic()

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                log_buffer.append(line)
                log_buffer_bytes += len(line)
                if (
                    time.monotonic() - last_flush > 1.0
                    or len(log_buffer) > 50
                    or log_buffer_bytes > 256 * 1024
                ):
                    flush_logs()

            flush_logs()
            batcher.flush()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                terminate_tree(proc, graceful_sec=5.0)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            exit_code = proc.returncode if proc.returncode is not None else -9
            try:
                append_entry(home, run_id, {"t": "exit", "code": exit_code, "finished_at": time.time()})
            except Exception:
                pass
            if stop_hard[0] and exit_code != 0:
                status = "interrupted"
            elif exit_code == 0:
                status = "completed"
            elif exit_code == 75:
                status = "suspended"
            else:
                status = "failed"

            print(f"[worker] Task finished with exit_code {exit_code} ({status})")

            # W7.6: post-task transfer (direction=pull: worker->master)
            if status == "completed":
                self._pull_transfers(transfer_specs, work_root, project, proj_name, run_id, finalize_token)

            self._upload_outputs(work_root, project, proj_name, task_start_time)

            _finalize(
                run_id,
                token=finalize_token,
                exit_code=exit_code,
                status=status,
                peak_ram_gib=round(peak_ram, 2),
                peak_vram_gib=round(peak_vram, 2),
                **({"stop_reason": "wall_clock_cap"} if stop_hard[0] else {}),
            )
            try:
                append_entry(home, run_id, {"t": "finalize", "payload": {"status": status, "exit_code": exit_code}})
                gc_journal(home, run_id)
            except Exception:
                pass

            time.sleep(1)
        except Exception as _task_exc:
            # W1.7: an unhandled exception in the task block must not crash the worker.
            # Log the error, finalize the run as failed, and continue to the next task.
            print(f"[worker] Unhandled exception in task {proj_name}/{task_id}: {_task_exc}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            try:
                _finalize(run_id, token=finalize_token, status="failed", exit_code=-1)
            except Exception:
                pass
        finally:
            # W1.7: always release stdout pipe to avoid fd leak.
            try:
                if proc is not None and proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            with slots_lock:
                slots.pop(rid, None)
