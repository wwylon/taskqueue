"""Per-slot process lifecycle management with OS-level containment.

Windows: wraps the child in a Job Object so the entire process tree dies
when the job handle is closed (KILL_ON_JOB_CLOSE).

Linux/macOS: starts the child in a new session (new pgid) so kill(-pgid)
reaches all descendants.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _create_job_object(kill_on_close: bool = True) -> wintypes.HANDLE:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        if not kill_on_close:
            return handle  # tree containment only; the task outlives the daemon (adopt_on_restart)

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(handle)
            raise OSError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")
        return handle

    def _assign_to_job(job_handle: wintypes.HANDLE, proc: subprocess.Popen) -> None:
        proc_handle = _kernel32.OpenProcess(0x1F0FFF, False, proc.pid)
        if not proc_handle:
            return
        try:
            _kernel32.AssignProcessToJobObject(job_handle, proc_handle)
        finally:
            _kernel32.CloseHandle(proc_handle)


_log = logging.getLogger(__name__)
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CTRL_BREAK_EVENT = 1


def spawn_with_guard(
    args: list[str], *, kill_on_close: bool = True, **popen_kwargs: Any
) -> tuple[subprocess.Popen, Any]:
    """Spawn a subprocess with OS-level containment.

    Returns (proc, guard_handle). Pass guard_handle to terminate_guarded().
    On Windows: guard_handle is a Job Object HANDLE (int); with ``kill_on_close`` (default) the
    tree dies when the daemon exits, without it the job only groups the tree for terminate_guarded.
    On Linux/macOS: guard_handle is the process group id (int).
    """
    if sys.platform == "win32":
        job = _create_job_object(kill_on_close)
        proc = subprocess.Popen(args, **popen_kwargs)
        # Console ctrl events can only be aimed at a process *group*; remember whether the child leads one.
        proc._tq_own_group = bool(int(popen_kwargs.get("creationflags", 0) or 0) & _CREATE_NEW_PROCESS_GROUP)  # type: ignore[attr-defined]
        try:
            _assign_to_job(job, proc)
        except Exception:  # noqa: BLE001 - job assignment is best-effort; the child still runs
            _log.debug("job assignment failed (best-effort; the child still runs)", exc_info=True)
        return proc, job
    else:
        # New session → new pgid; kill(-pgid) reaches all descendants
        popen_kwargs.setdefault("start_new_session", True)
        proc = subprocess.Popen(args, **popen_kwargs)
        pgid = os.getpgid(proc.pid)
        return proc, pgid


def terminate_guarded(
    proc: subprocess.Popen,
    guard: Any,
    graceful_sec: float = 5.0,
) -> None:
    """Terminate the process tree started with spawn_with_guard.

    On Windows: sends Ctrl+Break to the child's own process group (only when it was spawned with
    CREATE_NEW_PROCESS_GROUP), waits, then terminates the Job Object (kills tree).
    On Linux/macOS: sends SIGTERM to the process group, waits, then SIGKILL.
    """
    if sys.platform == "win32":
        # Never send CTRL_C_EVENT: Windows cannot limit it to one group and delivers it to *every*
        # process on the console (it killed pytest / the publish run sharing the terminal).
        if getattr(proc, "_tq_own_group", False):
            try:
                import ctypes as _ctypes

                _ctypes.windll.kernel32.GenerateConsoleCtrlEvent(_CTRL_BREAK_EVENT, proc.pid)
            except (AttributeError, OSError):
                pass
        deadline = time.monotonic() + graceful_sec
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        # Terminate the job (explicitly: a job without kill-on-close survives CloseHandle), then close it.
        if guard:
            try:
                import ctypes as _ctypes

                _k32 = _ctypes.WinDLL("kernel32")
                _k32.TerminateJobObject(guard, 1)  # also reaches grandchildren the root left behind
                _k32.CloseHandle(guard)
            except (AttributeError, OSError):
                pass
        # Final fallback
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        pgid = guard
        # SIGTERM to process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.monotonic() + graceful_sec
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass


def close_guard(guard: Any) -> None:
    """Release the guard handle without killing the process (use at finalize)."""
    if guard is None:
        return
    if sys.platform == "win32":
        try:
            import ctypes as _ctypes

            _ctypes.WinDLL("kernel32").CloseHandle(guard)
        except (AttributeError, OSError):
            pass
    # On POSIX, pgid is just an int — nothing to release
