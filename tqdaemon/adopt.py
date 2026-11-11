"""Adopt running tasks across a daemon restart (config ``adopt_on_restart``).

Adoptable runs start through ``tqdaemon.run_wrapper``: the task writes its own log file (no pipe to
the daemon), no kill-on-daemon-exit job, and the wrapper records the exit code in ``<log>.exit``.
After a restart the daemon finds each local ``running`` run whose wrapper is still alive (same pid
and create time, wrapper on the command line) or whose exit file exists, and keeps supervising it
with :class:`AdoptedProc` standing in for the ``Popen`` it no longer has. Anything else is still
reaped as an orphan.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil

WRAPPER_MODULE = "tqdaemon.run_wrapper"
# Runs started before/after the tqdaemon -> tqdaemon rename carry either module name on their cmdline.
WRAPPER_MODULES = ("tqdaemon.run_wrapper", "taskqueue.run_wrapper")
_KEEP_RUNS = "restart.keep-runs"


def exit_file_for(log_path: Path | str) -> Path:
    return Path(str(log_path) + ".exit")


def wrap_argv(argv: list[str], log_path: Path, exit_file: Path) -> list[str]:
    return [sys.executable, "-m", WRAPPER_MODULE, "--log", str(log_path), "--exit", str(exit_file), "--", *argv]


def read_exit_code(exit_file: Path | None) -> int | None:
    if exit_file is None:
        return None
    try:
        return int(exit_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _same_process(proc: psutil.Process, create_time: float | None) -> bool:
    if create_time is None:
        return True
    try:
        return abs(proc.create_time() - float(create_time)) < 1.0
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _still_active(pid: int) -> bool:
    """Has ``pid`` not exited? On Windows an exited process stays listed (and visible to psutil, create
    time and all) while anyone holds a handle to it, so ask for its exit code instead."""
    if sys.platform != "win32":
        return psutil.pid_exists(pid)
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def live_wrapper(pid: int | None, create_time: float | None) -> bool:
    """True when ``pid`` is still the tq wrapper this run started (pid-reuse safe)."""
    if not pid:
        return False
    try:
        if not _still_active(int(pid)):
            return False
        proc = psutil.Process(int(pid))
        if not _same_process(proc, create_time) or not proc.is_running():
            return False
        return any(m in part for part in proc.cmdline() for m in WRAPPER_MODULES)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False


class AdoptedProc:
    """The bits of ``subprocess.Popen`` ActiveRun uses, for a wrapper this daemon did not spawn."""

    stdout = None

    def __init__(self, pid: int, create_time: float | None, exit_file: Path) -> None:
        self.pid = int(pid)
        self._create_time = create_time
        self._exit_file = exit_file
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if live_wrapper(self.pid, self._create_time):
            return None
        code = read_exit_code(self._exit_file)
        self.returncode = code if code is not None else -1  # killed before it could record one
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import subprocess

        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout or 0)
            time.sleep(0.2)
        return self.returncode if self.returncode is not None else -1

    def _signal(self, kill: bool) -> None:
        try:
            proc = psutil.Process(self.pid)
            if _same_process(proc, self._create_time):
                proc.kill() if kill else proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def terminate(self) -> None:
        self._signal(False)

    def kill(self) -> None:
        self._signal(True)


def request_keep_runs(home: Path) -> None:
    """``tq restart``: ask the stopping daemon to leave adoptable runs alive for its successor."""
    (home / _KEEP_RUNS).write_text(str(int(time.time())), encoding="ascii")


def consume_keep_runs(home: Path, *, max_age_sec: float = 600.0) -> bool:
    """True (and clear the marker) when a recent ``tq restart`` asked to keep runs alive."""
    path = home / _KEEP_RUNS
    try:
        stamp = float(path.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return time.time() - stamp <= max_age_sec
