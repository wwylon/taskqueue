"""Subprocess lifecycle management for queue tasks.

Each ActiveRun wraps one running task process, streams its output to a log file,
parses (XX.X%) progress lines, and handles graceful stop. Tasks run with
cwd = their project's root.
"""

from __future__ import annotations
from taskqueue.jsonutil import json_list

import collections
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
import logging

import psutil

_log = logging.getLogger(__name__)

from taskqueue import config as tqconfig
from taskqueue._db_tasks import (
    clear_control_file,
    clear_stop_request,
    set_started_by,
    set_task_status,
    write_control_action,
)
from taskqueue._db_runs import finish_run, update_run_metrics, update_run_progress
from taskqueue.log_issues import note_progress, record_metric_samples, classify_log_line, empty_log_counts, merge_metrics_payload
from taskqueue.resources import (
    parse_json_list,
    parse_json_obj,
    sidecar_name_hints,
    task_requires_gpu,
)
_PCT_RE = re.compile(r"\((\d+(?:\.\d+)?)%\)")
_TQ_METRIC_RE = re.compile(r"^TQ_METRIC\s+(\{.+\})\s*$")
# W4.6: cpu_count never changes — cache it once at import time.
_CPU_COUNT: int = psutil.cpu_count(logical=True) or 1


def build_process_children_map() -> dict[int, list[int]]:
    """F-081: one ``process_iter`` → parent→children map for shared tree walks."""
    children: dict[int, list[int]] = {}
    try:
        for p in psutil.process_iter(["pid", "ppid"]):
            try:
                info = p.info
                pid = info.get("pid")
                ppid = info.get("ppid")
                if pid is None or ppid is None:
                    continue
                children.setdefault(int(ppid), []).append(int(pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
                continue
    except Exception:
        return {}
    return children


def _descendant_pids(root_pid: int, children_map: dict[int, list[int]]) -> list[int]:
    out: list[int] = []
    stack = list(children_map.get(root_pid, ()))
    seen = {root_pid}
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        stack.extend(children_map.get(c, ()))
    return out



def _parse_pct(line: str) -> float | None:
    m = _PCT_RE.search(line)
    return float(m.group(1)) if m else None


def preflight(command: Path, requires_paths: tuple[Path, ...]) -> str | None:
    """Return an error string if the command or a required path is missing."""
    cmd_str = str(command)
    if not command.is_file() and shutil.which(cmd_str) is None:
        return f"command not found: {command}"
    for req in requires_paths:
        if not req.exists():
            return f"missing required path: {req}"
    return None


class ActiveRun:
    """Manages a single running task subprocess."""

    def __init__(
        self,
        conn,  # sqlite3.Connection
        task_row,  # sqlite3.Row
        log_dir: Path,
        cwd: Path,
        graceful_stop_sec: int = 30,
        db_path: Path | None = None,
    ) -> None:
        self.conn = conn
        self._owner_thread = threading.get_ident()
        self._metrics_flushed_at = 0.0
        self.db_path = db_path
        self.project: str = task_row["project"]
        self.task_id: str = task_row["task_id"]
        self.task_row = task_row
        self.log_dir = log_dir
        self.cwd = cwd
        self.graceful_stop_sec = graceful_stop_sec

        self.run_id: int = 0
        self.proc: subprocess.Popen | None = None
        self._guard_handle: Any = None  # B3: Job Object (Win) or pgid (POSIX)
        self.progress_pct: float | None = None
        self.last_log_line: str | None = None
        self.last_line_time: float = 0.0
        self.started_at: float = 0.0
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._exit_code: int | None = None
        self.peak_ram_gib: float = 0.0
        self.peak_vram_gib: float = 0.0
        self.peak_cpu_pct: float = 0.0
        self.current_ram_gib: float = 0.0
        self.current_vram_gib: float = 0.0
        self.current_cpu_pct: float = 0.0
        # F-066: cumulative process CPU time (user+system) for usage accounting
        self.accumulated_cpu_seconds: float = 0.0
        self._flushed_cpu_seconds: float = 0.0
        self._prev_cpu_times_total: float | None = None
        self.stop_reason: str | None = None  # stop_requested | wall_clock_cap | ram_hard
        self._pid_create_time: float | None = None
        self._stop_in_progress: bool = False
        self.last_metrics: dict = {}  # R8: last seen TQ_METRIC values
        self.metric_series: dict[str, list] = {}  # numeric TQ_METRIC samples for the drawer sparkline
        self.suspend_requested_at: float | None = None
        self._psutil_procs: dict[int, psutil.Process] = {}
        self._last_history_ts: float = 0.0
        self._pending_history_sample: tuple | None = None
        # F-035: initialize before start() so exception handler doesn't AttributeError
        self._log_file = None
        self._log_path: Path | None = None
        self._tail_log: bool = False
        # Progress sanity (active CPU time)
        self.last_active_cpu_sec: float | None = None
        self.last_active_change_time: float = 0.0
        self._sanity_prev_pct: float | None = None
        self._sanity_prev_active: float | None = None
        # GPU utilization EMA (device-level %; updated by update_resources)
        self.gpu_util_ema: float | None = None  # None until first sample
        self._gpu_util_samples: int = 0
        self.current_gpu_util_pct: float | None = None
        # RSS trend detection: rolling window of (timestamp, rss_gib) samples, max 10 min at 10s/sample
        self._rss_window: collections.deque[tuple[float, float]] = collections.deque(maxlen=60)
        self._rss_leak_warned: bool = False  # avoid flooding the log
        keys = task_row.keys() if hasattr(task_row, "keys") else []
        self._task_resources = parse_json_obj(
            task_row["resources"] if "resources" in keys else {}
        )
        self._requires_services = parse_json_list(
            task_row["requires_services"] if "requires_services" in keys else []
        )
        self._gpu_hints = sidecar_name_hints(self._task_resources, self._requires_services)
        self._needs_gpu_probe = task_requires_gpu(self._task_resources) or bool(self._gpu_hints)
        self.log_counts = empty_log_counts()

    def _get_psutil_proc(self, pid: int) -> psutil.Process:
        """Return a cached psutil.Process for pid (creates on first access).

        Reusing the same object across calls is required for cpu_percent(interval=None)
        to return a meaningful value: the first call on a fresh Process object always
        returns 0.0 because there is no prior sample to diff against.
        """
        if pid not in self._psutil_procs:
            self._psutil_procs[pid] = psutil.Process(pid)
        return self._psutil_procs[pid]

    def _evict_dead_psutil_procs(self, live_pids: set[int]) -> None:
        """Remove cached Process objects for PIDs that are no longer alive."""
        dead = [pid for pid in self._psutil_procs if pid not in live_pids]
        for pid in dead:
            del self._psutil_procs[pid]

    def rss_growth_rate_gib_per_hour(self, min_samples: int = 6) -> float | None:
        """Estimate RSS growth rate (GiB/hour) via OLS over the rolling window.

        Returns None if the window is too short.  A positive result means RSS is
        trending upward; a negative result means it is shrinking or flat.

        Uses plain OLS (no numpy dependency) over at most 60 samples (~10 min).
        min_samples=6 requires at least ~1 minute of data before a result is given.
        """
        samples = list(self._rss_window)
        if len(samples) < min_samples:
            return None
        t0 = samples[0][0]
        xs = [s[0] - t0 for s in samples]
        ys = [s[1] for s in samples]
        n = len(xs)
        sx = sum(xs)
        sy = sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if denom == 0:
            return None
        slope_per_sec = (n * sxy - sx * sy) / denom
        return slope_per_sec * 3600.0  # convert to GiB/hour

    def _total_cpu_times(self, processes: list) -> float:
        total = 0.0
        for p in processes:
            try:
                t = self._get_psutil_proc(p.pid).cpu_times()
                total += float(t.user) + float(t.system)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def _accumulate_cpu_times(self, processes: list) -> None:
        """F-066: advance accumulated_cpu_seconds from cpu_times() deltas."""
        total = self._total_cpu_times(processes)
        if self._prev_cpu_times_total is not None:
            delta = total - self._prev_cpu_times_total
            if delta > 0:
                self.accumulated_cpu_seconds += delta
        self._prev_cpu_times_total = total

    def drain_cpu_seconds(self) -> float:
        """Return CPU-seconds since last drain and mark them flushed."""
        delta = max(0.0, self.accumulated_cpu_seconds - self._flushed_cpu_seconds)
        self._flushed_cpu_seconds = self.accumulated_cpu_seconds
        return delta

    def start(self, *, use_resume: bool) -> str | None:
        """Start the subprocess. Returns error string on preflight failure, else None."""
        from taskqueue.platform_utils import pythonw_if_available, resolve_command

        command = pythonw_if_available(resolve_command(self.task_row["command"], self.cwd))
        requires_raw = json_list(self.task_row["requires_paths"])
        requires = [Path(p) if Path(p).is_absolute() else self.cwd / p for p in requires_raw]
        err = preflight(command, tuple(requires))
        if err:
            return err

        argv_tail, args_override_raw, err = self._resolve_argv_tail(use_resume)
        if err:
            return err
        argv = [str(command), *argv_tail]
        log_path = self._unique_log_path()

        from taskqueue.db import claim_and_insert_local_run, clear_task_error

        try:
            # F-088: claim + insert_run in one BEGIN IMMEDIATE (no running-without-run window).
            run_id = claim_and_insert_local_run(
                self.conn,
                self.project,
                self.task_id,
                str(log_path),
                clear_args_override=bool(args_override_raw),
            )
            if run_id is None:
                return "task already claimed (not queued)"
            self.run_id = run_id
            clear_task_error(self.conn, self.project, self.task_id)
            self.started_at = time.monotonic()

            self._log_path = log_path
            self._tail_log = False
            # Create/truncate the log, then reopen for parent messages (append).
            with open(log_path, "w", encoding="utf-8", errors="replace"):
                pass
            self._log_file = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            self._spawn(argv, self._child_env(), log_path)
            self._thread = threading.Thread(target=self._stream_output, daemon=True)
            self._thread.start()
            self._record_pid()
            self._notify_started()
            return None
        except Exception as exc:
            return self._fail_start(f"start failed after claim: {exc}")

    def _resolve_argv_tail(self, use_resume: bool) -> tuple[list, str | None, str | None]:
        """(argv tail, raw one-shot override, error). R2: a `tq retry --set` override wins."""
        from taskqueue.db import clear_args_override

        args_override_raw = self.task_row["args_override"] if "args_override" in self.task_row.keys() else None
        if args_override_raw:
            try:
                argv_tail = json.loads(args_override_raw)
                if not isinstance(argv_tail, list):
                    raise ValueError("not a JSON list")
            except ValueError as exc:
                # Bad one-shot override: clear it so the next attempt is not wedged,
                # and fail this start rather than silently running the old args.
                clear_args_override(self.conn, self.project, self.task_id)
                return [], None, f"invalid args_override ({exc}); cleared — re-run tq retry --set"
            return argv_tail, args_override_raw, None
        args_key = "resume_args" if use_resume and self.task_row["resume_args"] else "args"
        return json_list(self.task_row[args_key]), None, None

    def _unique_log_path(self) -> Path:
        ts = time.strftime("%Y%m%dT%H%M%S")
        log_path = self.log_dir / f"{self.task_id}_{ts}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Two starts in the same second must not truncate the earlier run's log.
        _n = 1
        while log_path.exists():
            log_path = self.log_dir / f"{self.task_id}_{ts}_{_n}.log"
            _n += 1
        return log_path

    def _child_env(self) -> dict[str, str]:
        """Environment for the task: control-file variables, operator, then the task's env: block
        (which may not override the TQ_* control variables)."""
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # Cooperative control (Slice Client)
        home = Path(self.db_path).parent if self.db_path else tqconfig.state_home()
        ctrl = tqconfig.control_path(home, self.project, self.task_id)
        tqconfig.controls_dir(home).mkdir(parents=True, exist_ok=True)
        if not ctrl.exists():
            ctrl.write_text("{}", encoding="utf-8")
        env["TQ_HOME"] = str(home)
        env["TQ_PROJECT"] = self.project
        env["TQ_TASK_ID"] = self.task_id
        env["TQ_CONTROL_FILE"] = str(ctrl)
        if os.environ.get("TQ_OPERATOR"):
            env["TQ_OPERATOR"] = os.environ["TQ_OPERATOR"]
            set_started_by(self.conn, self.project, self.task_id, os.environ["TQ_OPERATOR"])

        # Merge task-level env overrides (from YAML env: block).
        # F-128 slim admit rows omit ``env`` — sqlite3.Row raises IndexError (not KeyError).
        task_env = getattr(self, "task_row", None)
        if task_env is not None:
            try:
                from taskqueue.rows import row_get

                _task_env_raw = row_get(task_env, "env")
                if _task_env_raw:
                    _task_env = json.loads(_task_env_raw)
                    if isinstance(_task_env, dict):
                        _reserved = {"TQ_HOME", "TQ_PROJECT", "TQ_TASK_ID", "TQ_CONTROL_FILE"}
                        env.update({
                            str(k): str(v) for k, v in _task_env.items()
                            if str(k) not in _reserved
                        })
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        return env

    def _spawn(self, argv: list, env: dict[str, str], log_path: Path) -> None:
        """Start the child under the kill-on-close guard.

        Windows: CREATE_NO_WINDOW + PIPE hung under a windowless daemon, so the child
        writes straight to the log file and the parent tails it. Also inject
        sitecustomize so nested Python->go/cmake spawns stay hidden.
        """
        from taskqueue.platform_proc import spawn_with_guard
        from taskqueue.win_console import (
            apply_hide_console_popen_kwargs,
            inject_python_hide_console,
        )

        popen_kwargs: dict = {
            "args": argv,
            "cwd": str(self.cwd),
            "stdin": subprocess.DEVNULL,
            "env": inject_python_hide_console(env),
        }
        if os.name == "nt":
            # Child writes raw lines; parent tails for progress / idle / cap.
            child_out = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            popen_kwargs["stdout"] = child_out
            popen_kwargs["stderr"] = subprocess.STDOUT
            popen_kwargs = apply_hide_console_popen_kwargs(popen_kwargs)
            _argv = popen_kwargs.pop("args")
            try:
                self.proc, self._guard_handle = spawn_with_guard(_argv, **popen_kwargs)
            finally:
                try:
                    child_out.close()
                except Exception:
                    pass
            self._tail_log = True
        else:
            popen_kwargs["stdout"] = subprocess.PIPE
            popen_kwargs["stderr"] = subprocess.STDOUT
            popen_kwargs["text"] = True
            popen_kwargs["encoding"] = "utf-8"
            popen_kwargs["errors"] = "replace"
            popen_kwargs["bufsize"] = 1
            _argv = popen_kwargs.pop("args")
            self.proc, self._guard_handle = spawn_with_guard(_argv, **popen_kwargs)

    def _record_pid(self) -> None:
        """Store pid + create time (pid-reuse safe kills after a daemon restart)."""
        if self.proc is None or self.run_id is None:
            return
        from taskqueue.db import set_run_pid

        create_time: float | None = None
        try:
            create_time = psutil.Process(self.proc.pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            create_time = None
        self._pid_create_time = create_time
        set_run_pid(self.conn, self.run_id, self.proc.pid, create_time=create_time)

    def _notify_started(self) -> None:
        """Started notification (toast + optional webhook). Never fails the start."""
        try:
            from taskqueue.db import get_config
            from taskqueue.notify import notify_task_started
            from taskqueue.rows import row_get

            _home = Path(self.db_path).parent if self.db_path else tqconfig.state_home()
            _cfg = get_config(self.conn)
            _tr = getattr(self, "task_row", None)

            def _opt(key: str) -> str | None:
                return (row_get(_tr, key) or None) if _tr is not None else None

            notify_task_started(
                _home,
                project=self.project,
                task_id=self.task_id,
                run_id=self.run_id,
                started_by=_opt("started_by"),
                notify_url=_opt("notify_url"),
                allow_remote=bool(_cfg.get("notify_allow_remote")),
                cfg=_cfg,
                description=_opt("description"),
                suite=_opt("suite"),
            )
        except Exception as _exc:
            print(f"[runner] warn: notify_task_started failed: {_exc}", file=__import__("sys").stderr)

    def _fail_start(self, err: str) -> str:
        """A step after claim raised: kill any child, fail the run and task, close the log."""
        from taskqueue import errors as tqerr
        from taskqueue.db import set_task_error

        # A step after spawn raised: do not leave the child running unsupervised.
        if self.proc is not None:
            try:
                from taskqueue.reap import kill_process_tree

                kill_process_tree(self.proc.pid, graceful_sec=1.0)
            except Exception:
                pass
        if self.run_id:
            finish_run(
                self.conn,
                self.run_id,
                -1,
                "failed",
                last_error_code=tqerr.RUN_PREFLIGHT,
                only_if_running=True,
            )
        set_task_status(self.conn, self.project, self.task_id, "failed")
        set_task_error(
            self.conn, self.project, self.task_id, tqerr.RUN_PREFLIGHT, block_reason=err
        )
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        return err

    def idle_sec(self) -> float:
        """Seconds since the last line of output was received."""
        if self.last_line_time == 0.0:
            return time.monotonic() - self.started_at
        return time.monotonic() - self.last_line_time

    def active_cpu_sec(self) -> float | None:
        from taskqueue.progress_sanity import process_active_cpu_sec

        pid = self.proc.pid if self.proc else None
        return process_active_cpu_sec(pid)

    def refresh_active_cpu(self) -> float | None:
        """Sample process CPU; update last_active_change_time when it moves."""
        cur = self.active_cpu_sec()
        now = time.monotonic()
        if cur is None:
            return None
        if self.last_active_cpu_sec is None:
            self.last_active_cpu_sec = cur
            self.last_active_change_time = now
            return cur
        if cur > self.last_active_cpu_sec + 0.01:
            self.last_active_change_time = now
            self.last_active_cpu_sec = cur
        else:
            self.last_active_cpu_sec = cur
        return cur

    def active_idle_sec(self) -> float:
        """Seconds since process CPU time last increased meaningfully."""
        if self.last_active_change_time == 0.0:
            if self.started_at:
                return time.monotonic() - self.started_at
            return 0.0
        return time.monotonic() - self.last_active_change_time

    def _stream_output(self) -> None:
        if getattr(self, "_tail_log", False):
            self._stream_output_tail()
        else:
            self._stream_output_pipe()

    def _handle_output_line(self, line: str, *, stamped: bool, cap) -> bool:
        """Process one output line. Returns True if wall-clock cap triggered stop."""
        line = line.rstrip("\n\r")
        if stamped:
            ts = time.strftime("%H:%M:%S")
            out = f"[{ts}] {line}"
            self._log_file.write(out + "\n")
            self._log_file.flush()
        self.last_log_line = line
        self.last_line_time = time.monotonic()
        pct = _parse_pct(line)
        if pct is not None:
            if pct != self.progress_pct:
                self._progress_timing = note_progress(getattr(self, "_progress_timing", None), self.progress_pct, pct, time.time())
            self.progress_pct = pct
        kind = classify_log_line(line)
        if kind:
            self.log_counts[kind] = self.log_counts.get(kind, 0) + 1
        m = _TQ_METRIC_RE.match(line)
        if m:
            try:
                new_metrics = json.loads(m.group(1))
                if isinstance(new_metrics, dict):
                    new_metrics.pop("log", None)
                    new_metrics.pop("_series", None)
                    self.last_metrics.update(new_metrics)
                    record_metric_samples(self.metric_series, new_metrics, time.time())
                    self._flush_metrics_json()
            except Exception:
                pass
        if cap and (time.monotonic() - self.started_at) > cap:
            return self._enforce_wall_clock_cap()
        return False

    def _enforce_wall_clock_cap(self) -> bool:
        """F-087: cooperative suspend then terminate on wall_clock_cap.

        Shared by the line path and the Windows idle (no-new-line) path so
        ``auto_retry_on_cap`` tasks get the exit-75 checkpoint window.
        Also writes the cap-reached log line so callers don't duplicate it.
        """
        # F-036: set stop_reason before terminate so daemon finalize uses
        # correct status (interrupted/tq.run.cap, not failed/tq.run.nonzero)
        self.stop_reason = self.stop_reason or "wall_clock_cap"
        # H2: write termination notice here so both call sites share one copy.
        try:
            cap = self.task_row["wall_clock_cap_sec"] if self.task_row else None
        except (KeyError, IndexError, TypeError):
            cap = None
        try:
            self._log_file.write(
                f"[{time.strftime('%H:%M:%S')}] [queue] wall_clock_cap_sec={cap} "
                f"reached — terminating\n"
            )
            self._log_file.flush()
        except Exception:
            pass
        # ds2 §1.4 / F-087: write the cooperative-suspend control file first.
        if self.db_path:
            try:
                _home = Path(self.db_path).parent
                write_control_action(_home, self.project, self.task_id, "suspend")
                _grace = min(5.0, float(self.graceful_stop_sec))
                _deadline = time.monotonic() + _grace
                while self.proc and self.proc.poll() is None and time.monotonic() < _deadline:
                    time.sleep(0.1)
            except Exception:
                pass
        self._terminate()
        return True

    def _stream_output_tail(self) -> None:
        """Windows path: child writes the log file; parent tails for progress."""
        assert self.proc is not None
        cap = self.task_row["wall_clock_cap_sec"]
        crashed = False
        try:
            pending = ""  # partial line the child has not finished writing yet
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as rf:
                while True:
                    line = rf.readline()
                    if line:
                        if not line.endswith("\n"):
                            pending += line
                            time.sleep(0.05)
                            continue
                        line, pending = pending + line, ""
                        if self._handle_output_line(line, stamped=False, cap=cap):
                            break
                        continue
                    if self.proc.poll() is not None:
                        while True:
                            line = rf.readline()
                            if not line:
                                break
                            line, pending = pending + line, ""
                            if self._handle_output_line(line, stamped=False, cap=cap):
                                break
                        if pending:
                            self._handle_output_line(pending, stamped=False, cap=cap)
                        break
                    if cap and (time.monotonic() - self.started_at) > cap:
                        self._enforce_wall_clock_cap()
                        break
                    time.sleep(0.05)
        except Exception as exc:
            crashed = True
            try:
                self._log_file.write(
                    f"[{time.strftime('%H:%M:%S')}] [queue] reader thread crashed: {exc!r} -- "
                    f"terminating and finalizing run\n"
                )
            except Exception:
                pass

        self._finish_stream(crashed)

    def _stream_output_pipe(self) -> None:
        assert self.proc and self.proc.stdout
        cap = self.task_row["wall_clock_cap_sec"]
        crashed = False
        try:
            for line in self.proc.stdout:
                if self._handle_output_line(line, stamped=True, cap=cap):
                    break
        except Exception as exc:
            # A crash here (e.g. a stdout decode error -- see the explicit
            # encoding="utf-8" added to Popen() above, added after exactly
            # this class of crash silently killed this thread while
            # translating non-ASCII UI strings) must never leave the run
            # wedged in "running" forever the way it did before: nothing
            # else sets self._done, so poll() never returns True, finalize()
            # never runs, and stop_requested is never cleared. Record the
            # failure in the task's own log, then always fall through to
            # shutdown below.
            crashed = True
            try:
                self._log_file.write(
                    f"[{time.strftime('%H:%M:%S')}] [queue] reader thread crashed: {exc!r} -- "
                    f"terminating and finalizing run\n"
                )
            except Exception:
                pass

        self._finish_stream(crashed)

    def _finish_stream(self, crashed: bool) -> None:
        assert self.proc is not None
        self.reader_crashed = bool(crashed)
        if crashed:
            # We can no longer safely keep reading this process's stdout (the
            # decode itself is what failed), so if it's still alive it may
            # now be blocked writing into a pipe nobody is draining anymore.
            # Terminate rather than risk proc.wait() below hanging forever.
            self._terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                    self.proc.wait()
                except Exception:
                    pass

        try:
            if not crashed:
                self.proc.wait()
            self._exit_code = self.proc.returncode
        except Exception:
            self._exit_code = -1
        if self._exit_code is None:
            self._exit_code = -1
        try:
            self._log_file.close()
        except Exception:
            pass
        self._done.set()
        from taskqueue.sched_wake import signal_run_finished
        signal_run_finished()

    def _terminate(self) -> None:
        """Request exit of the whole process tree (not only the wrapper PID)."""
        if self.proc and self.proc.poll() is None:
            guard = getattr(self, "_guard_handle", None)
            graceful = min(5.0, float(self.graceful_stop_sec))
            if guard is not None:
                from taskqueue.platform_proc import close_guard, terminate_guarded

                terminate_guarded(self.proc, guard, graceful_sec=graceful)
                close_guard(guard)
                self._guard_handle = None
            else:
                from taskqueue.reap import kill_process_tree

                kill_process_tree(
                    self.proc.pid,
                    create_time=getattr(self, "_pid_create_time", None),
                    graceful_sec=graceful,
                )

    def poll(self) -> bool:
        """Return True if the run has finished."""
        return self._done.is_set()

    def elapsed_sec(self) -> float:
        return time.monotonic() - self.started_at

    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def finalize(self, status: str, *, last_error_code: str | None = None, stopped_by: str | None = None, only_if_running: bool = False) -> None:
        """Called when the run is done. Writes final DB state."""
        from taskqueue.db import set_task_error
        from taskqueue.notify import emit_task_notify

        code = self._exit_code if self._exit_code is not None else -1

        # W6.10: artifact verification — check declared paths after a successful exit.
        if status == "completed" and not last_error_code:
            try:
                _arts_raw = self.task_row["artifacts"] if "artifacts" in self.task_row.keys() else None
                _artifacts = json_list(_arts_raw)
                _req = bool(self.task_row["artifacts_required"]) if "artifacts_required" in self.task_row.keys() else False
                # F-038: resolve artifact paths against project root, not daemon CWD
                _proj_root = self.cwd
                missing = [p for p in _artifacts if not (_proj_root / p).exists()]
                if missing:
                    _artifact_msg = f"artifact(s) missing: {', '.join(missing)}"
                    if self._log_file:
                        try:
                            self._log_file.write(f"[tq] WARNING: {_artifact_msg}\n")
                        except Exception:
                            pass
                    if _req:
                        status = "failed"
                        last_error_code = "tq.run.artifacts_missing"
            except Exception:
                pass

        self.update_db_progress_light(conn=self.conn)
        from taskqueue.db import finalize_run_and_task

        if not finalize_run_and_task(
            self.conn,
            self.run_id,
            self.project,
            self.task_id,
            code,
            status,
            last_error_code=last_error_code,
            peak_ram_gib=self.peak_ram_gib,
            peak_vram_gib=self.peak_vram_gib,
            peak_cpu_pct=self.peak_cpu_pct,
            stopped_by=stopped_by,
            stop_reason=self.stop_reason,
            only_if_running=only_if_running,
        ):
            return
        if status == "completed":
            try:
                self.conn.execute(
                    "UPDATE tasks SET cap_auto_retry_count = 0 WHERE project = ? AND task_id = ?",
                    (self.project, self.task_id),
                )
                self.conn.commit()
            except Exception:
                pass
        if last_error_code:
            # Preserve a rich block_reason already set by the daemon (e.g. RAM/VRAM stop).
            prev = self.conn.execute(
                "SELECT block_reason FROM tasks WHERE project = ? AND task_id = ?",
                (self.project, self.task_id),
            ).fetchone()
            br = prev["block_reason"] if prev else None
            set_task_error(
                self.conn, self.project, self.task_id, last_error_code, block_reason=br
            )
        elif status in ("completed", "suspended"):
            set_task_error(self.conn, self.project, self.task_id, None, block_reason=None)
        clear_stop_request(self.conn, self.project, self.task_id)
        if self.db_path:
            clear_control_file(Path(self.db_path).parent, self.project, self.task_id)

        home, note, stop_reason = self._finalize_notify_context(code, last_error_code)
        emit_task_notify(home, status=status, stop_reason=stop_reason, **note)

        # on_complete / on_fail / rearm_on / teardown (shared with remote finalize)
        from taskqueue.finalize_hooks import apply_finalize_wake_hooks

        apply_finalize_wake_hooks(
            self.conn,
            self.project,
            self.task_id,
            status,
            task_row=self.task_row,
            run_teardown=True,
        )

        # notify_on_attention: fire webhook when task lands on attention list.
        try:
            if bool(note["cfg"].get("notify_on_attention")) and status in (
                "failed", "interrupted", "blocked",
            ):
                emit_task_notify(home, status=f"attention:{status}", **note)
        except Exception:
            pass

    def _finalize_notify_context(self, code: int | None, last_error_code: str | None) -> tuple[Path, dict, str | None]:
        """(home, emit_task_notify kwargs, stop_reason) for the terminal notifications."""
        from taskqueue.db import get_config
        from taskqueue.rows import row_get

        tr = getattr(self, "task_row", None)

        def _opt(key: str) -> Any:
            try:
                return (row_get(tr, key) or None) if tr is not None else None
            except (KeyError, IndexError, TypeError):
                return None

        notify_url = _opt("notify_url")
        started_by = _opt("started_by")
        # Prefer live DB row (started_by may have been set at start).
        row = self.conn.execute(
            "SELECT notify_url, started_by FROM tasks WHERE project = ? AND task_id = ?",
            (self.project, self.task_id),
        ).fetchone()
        if row is not None:
            notify_url = row["notify_url"]
            started_by = row["started_by"]
        home = Path(self.db_path).parent if self.db_path else tqconfig.state_home()
        cfg = get_config(self.conn)
        # R9: prefer in-memory stop_reason (cap/RAM/evict); fall back to CLI --reason
        stop_reason: str | None = self.stop_reason
        if not stop_reason and self.run_id:
            try:
                _sr_row = self.conn.execute(
                    "SELECT stop_reason FROM runs WHERE run_id = ?", (self.run_id,)
                ).fetchone()
                if _sr_row:
                    stop_reason = _sr_row[0] or None
            except Exception:
                pass
        note = {
            "project": self.project,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "exit_code": code,
            "last_error_code": last_error_code,
            "notify_url": notify_url,
            "started_by": started_by,
            "allow_remote": bool(cfg.get("notify_allow_remote")),
            "cfg": cfg,
            "description": _opt("description"),
            "suite": _opt("suite"),
            "elapsed_sec": (time.monotonic() - self.started_at) if self.started_at else None,
            "peak_ram_gib": self.peak_ram_gib if self.peak_ram_gib else None,
            "progress_pct": self.progress_pct,
            "block_reason": _opt("block_reason"),
        }
        return home, note, stop_reason

    def request_cooperative_suspend(self, home: Path) -> None:
        """Ask the task to exit 75 via control file (no SIGTERM yet).

        Idempotent: the grace-period clock starts on the first call and
        is not reset by subsequent calls in later scheduler ticks.
        """
        if self.suspend_requested_at is None:
            self.suspend_requested_at = time.monotonic()
        write_control_action(home, self.project, self.task_id, "suspend")

    def suspend_grace_elapsed(self, grace_sec: float) -> bool:
        """True once `grace_sec` has passed since the first cooperative-suspend
        request, or immediately if suspend was never requested (nothing to
        wait for -- caller should proceed straight to stop())."""
        if self.suspend_requested_at is None:
            return True
        return (time.monotonic() - self.suspend_requested_at) >= grace_sec

    def stop(self, *, background: bool = False) -> None:
        """Graceful stop of the process tree: SIGTERM, wait, then SIGKILL.

        ``background=True`` (scheduler tick) runs the reap on a thread so one
        stopping run cannot hold the tick for graceful_stop_sec + 30 s.

        Always reaps descendants. Tasks launched via ``cmd /c`` (or similar)
        keep RAM in the grandchild Python — killing only the wrapper PID left
        that RSS allocated after ``tq.stop.ram`` / UI stop.

        Host RAM/VRAM eviction runs the reap on a background thread so the
        daemon tick (and HTTP) are not blocked for multi-second kills.

        After the process itself has exited, the run isn't finalized until
        the reader thread's `for line in self.proc.stdout:` loop (see
        _stream_output) observes EOF and sets self._done. On Windows this
        occasionally does not happen promptly after a forced kill -- the
        blocking pipe read can outlive the process it was reading from --
        which otherwise wedges the run in "running" forever: the scheduler
        loop re-calls stop() every tick, proc.poll() is already non-None so
        it returns instantly, and nothing ever clears stop_requested. See
        _unblock_reader_if_stuck for the bounded recovery.
        """
        if self._stop_in_progress:
            _t = getattr(self, "_reap_thread", None)
            if _t is not None and _t.is_alive():
                return  # the background reap owns this stop; do not block the tick
            self._unblock_reader_if_stuck()
            return
        if not self.proc or self.proc.poll() is not None:
            self._unblock_reader_if_stuck()
            return
        self._stop_in_progress = True
        from taskqueue.reap import kill_process_tree

        # Host RAM/VRAM eviction must free RSS quickly; a long graceful wait
        # leaves the machine thrashing while the victim still holds memory.
        resource_evict = self.stop_reason in ("ram_hard", "vram_hard")
        grace = 0.0 if resource_evict else float(self.graceful_stop_sec)
        pid = self.proc.pid
        create_time = getattr(self, "_pid_create_time", None)

        def _reap() -> None:
            try:
                kill_process_tree(pid, create_time=create_time, graceful_sec=grace)
                try:
                    if self.proc is not None:
                        self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    try:
                        if self.proc is not None:
                            self.proc.kill()
                    except Exception:
                        pass
                    try:
                        if self.proc is not None:
                            self.proc.wait(timeout=2)
                    except Exception:
                        pass
                self._done.wait(timeout=5)
                self._unblock_reader_if_stuck()
            except Exception as exc:
                print(f"[queue] stop reap failed pid={pid}: {exc}", flush=True)
                try:
                    self._unblock_reader_if_stuck()
                except Exception:
                    pass

        if resource_evict or background:
            import threading

            self._reap_thread = threading.Thread(
                target=_reap, daemon=True, name=f"tq-stop-{pid}"
            )
            self._reap_thread.start()
            return

        _reap()

    def _unblock_reader_if_stuck(self) -> None:
        """If the subprocess has exited but the reader thread hasn't noticed
        within a few seconds, force-close its stdout pipe (closing the read
        handle typically unblocks a pending ReadFile on Windows even when
        process termination alone did not), then wait once more. If the
        reader thread is still wedged after that, finalize state directly
        so the run doesn't loop forever -- the reader thread is a daemon
        thread and is abandoned; it will be cleaned up on interpreter exit."""
        if self._done.is_set():
            return
        if self._done.wait(timeout=2):
            return
        try:
            if self.proc and self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass
        if self._done.wait(timeout=3):
            return
        if self._exit_code is None:
            code = self.proc.poll() if self.proc else None
            self._exit_code = code if code is not None else -1
        try:
            if self._log_file:
                self._log_file.close()
        except Exception:
            pass
        self._done.set()
        from taskqueue.sched_wake import signal_run_finished
        signal_run_finished()

    def _sample_tree(self, children_map: dict[int, list[int]] | None = None) -> set[int] | None:
        """Sum RSS and CPU % over the process tree and update current/peak values.

        Returns the live pids (None when there is no process). With ``children_map``
        (one tick-wide ``build_process_children_map``, F-081) descendants are walked in
        memory instead of ``Process.children(recursive=True)`` per run.
        """
        if not self.proc:
            return None
        pid = self.proc.pid
        ram_bytes = 0
        cpu_pct = 0.0
        live_pids: set[int] = set()
        try:
            parent = self._get_psutil_proc(pid)
            live_pids.add(pid)
            if children_map is not None:
                procs = [parent]
                for cpid in _descendant_pids(pid, children_map):
                    try:
                        procs.append(self._get_psutil_proc(cpid))
                        live_pids.add(cpid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            else:
                children = parent.children(recursive=True)
                procs = [parent] + children
                live_pids.update(c.pid for c in children)
            for p in procs:
                try:
                    ram_bytes += p.memory_info().rss
                    cpu_pct += self._get_psutil_proc(p.pid).cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self._accumulate_cpu_times(procs)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        self._evict_dead_psutil_procs(live_pids)
        ram_gib = ram_bytes / (1024 ** 3)
        self.current_ram_gib = ram_gib
        self.peak_ram_gib = max(self.peak_ram_gib, ram_gib)
        self.current_cpu_pct = cpu_pct / _CPU_COUNT
        self.peak_cpu_pct = max(self.peak_cpu_pct, self.current_cpu_pct)
        return live_pids

    def refresh_ram_cpu(self, children_map: dict[int, list[int]] | None = None) -> None:
        """Fast psutil-only sample of RAM and CPU — called every scheduler tick.

        No nvidia-smi subprocess here; VRAM is updated on the slower
        update_resources() heartbeat path.
        """
        if self._sample_tree(children_map) is None:
            return
        # C2: record resource history sample every 10s
        now = time.time()
        if now - self._last_history_ts >= 10:
            self._pending_history_sample = (
                self.current_cpu_pct,
                self.current_ram_gib,
                self.current_vram_gib,
                self.current_gpu_util_pct,
            )
            self._last_history_ts = now
            self._rss_window.append((now, self.current_ram_gib))

    def update_resources(self) -> None:
        """Measure current resource usage of the process and children, and update peaks."""
        live_pids = self._sample_tree()
        if live_pids is None:
            return

        if not self._needs_gpu_probe:
            return

        from taskqueue.resources import (
            attributed_gpu_pids,
            attributed_vram_gib,
            probe_gpu_compute_apps,
            probe_gpu_memory_used,
            resolve_nvidia_smi,
            shared_gpu_probe,
        )
        from taskqueue.win_console import hide_console_check_output_kwargs  # noqa: PLC0415

        probe = shared_gpu_probe("apps", lambda: probe_gpu_compute_apps(timeout=2.0))
        if not probe.ok:
            # Keep previous current_vram — a failed nvidia-smi is not 0 GiB.
            return
        device_used = None
        attributed = attributed_gpu_pids(probe.processes, live_pids, self._gpu_hints)
        if attributed and not any(float(p.get("vram_gib") or 0) > 0 for p in attributed):
            device_used = shared_gpu_probe("mem", lambda: probe_gpu_memory_used(timeout=2.0))
        vram_gib = attributed_vram_gib(
            probe.processes, live_pids, self._gpu_hints, device_used_gib=device_used
        )
        self.current_vram_gib = vram_gib
        self.peak_vram_gib = max(self.peak_vram_gib, vram_gib)

        # Device-level GPU % when this task (tree or sidecar) actually holds VRAM.
        # Two concurrent GPU tasks share the same device % — accepted this pass.
        if vram_gib <= 0:
            self.current_gpu_util_pct = None
            return
        try:
            smi = resolve_nvidia_smi()
            if not smi:
                return
            util_out = shared_gpu_probe("util", lambda: subprocess.check_output(
                [smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                timeout=2,
                text=True,
                stderr=subprocess.DEVNULL,
                **hide_console_check_output_kwargs(),
            ))
            lines = [ln.strip() for ln in util_out.strip().splitlines() if ln.strip()]
            if lines:
                util_pct = float(lines[0])
                self.current_gpu_util_pct = util_pct
                if self.gpu_util_ema is None:
                    self.gpu_util_ema = util_pct
                else:
                    self.gpu_util_ema = 0.3 * util_pct + 0.7 * self.gpu_util_ema
                self._gpu_util_samples += 1
        except Exception:
            pass

    def update_db_progress_light(self, conn=None) -> None:
        """Flush cached peaks/progress without nvidia-smi (finalize hot path)."""
        self._write_progress(conn)

    def update_db_progress(self, conn=None) -> None:
        """Heartbeat: sample resources (incl. GPU), then persist progress and peaks."""
        self.update_resources()
        self._write_progress(conn)

    def _write_progress(self, conn=None) -> None:
        """Persist progress/peaks and metrics. Without ``conn`` a short-lived one is used."""
        close_conn = False
        if conn is None:
            if self.db_path:
                import sqlite3
                conn = sqlite3.connect(str(self.db_path), timeout=10)
                close_conn = True
            else:
                conn = self.conn
        try:
            update_run_progress(
                conn,
                self.run_id,
                self.progress_pct,
                self.last_log_line,
                self.peak_ram_gib,
                self.peak_vram_gib,
                self.peak_cpu_pct,
            )
            self._flush_metrics_json(conn=conn)
        finally:
            if close_conn and conn:
                conn.close()

    def _flush_metrics_json(self, conn=None) -> None:
        """Persist TQ_METRIC fields plus reserved log counts (heartbeat, not per line).

        Called from the output reader thread when conn is None: sqlite connections
        must not be shared across threads, so that path uses its own short-lived one.
        """
        if not self.run_id:
            return
        if conn is None:
            # Reader-thread path: at most ~1 write/s; the heartbeat flush (with conn) persists the rest.
            now = time.monotonic()
            if now - getattr(self, "_metrics_flushed_at", 0.0) < 1.0:
                return
            self._metrics_flushed_at = now
        payload = merge_metrics_payload(self.last_metrics, self.log_counts, getattr(self, "metric_series", None),
                                        getattr(self, "_progress_timing", None))
        if not payload:
            return
        own = None
        db = conn
        try:
            if db is None:
                if self.db_path and threading.get_ident() != self._owner_thread:
                    import sqlite3

                    own = db = sqlite3.connect(str(self.db_path), timeout=10)
                else:
                    db = self.conn
            if db is None:
                return
            update_run_metrics(db, self.run_id, payload)
        except Exception as exc:
            _log.warning("metrics flush failed for run %s: %s", self.run_id, exc)
        finally:
            if own is not None:
                own.close()
