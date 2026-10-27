"""Per-slot process lifecycle management with OS-level containment.

Windows: wraps the child in a Job Object so the entire process tree dies
when the job handle is closed (KILL_ON_JOB_CLOSE).

Linux/macOS: starts the child in a new session (new pgid) so kill(-pgid)
reaches all descendants.
"""
from __future__ import annotations

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

    def _create_job_object() -> wintypes.HANDLE:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")

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


def spawn_with_guard(args: list[str], **popen_kwargs: Any) -> tuple[subprocess.Popen, Any]:
    """Spawn a subprocess with OS-level containment.

    Returns (proc, guard_handle). Pass guard_handle to terminate_guarded().
    On Windows: guard_handle is a Job Object HANDLE (int).
    On Linux/macOS: guard_handle is the process group id (int).
    """
    if sys.platform == "win32":
        job = _create_job_object()
        proc = subprocess.Popen(args, **popen_kwargs)
        try:
            _assign_to_job(job, proc)
        except Exception:
            pass  # best-effort; still useful even if assignment fails
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

    On Windows: sends Ctrl+C first, waits, then closes the Job Object (kills tree).
    On Linux/macOS: sends SIGTERM to the process group, waits, then SIGKILL.
    """
    if sys.platform == "win32":
        # Try graceful first
        try:
            import ctypes as _ctypes
            _ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, proc.pid)  # CTRL_C_EVENT
        except Exception:
            pass
        deadline = time.monotonic() + graceful_sec
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        # Close Job Object — OS kills entire tree
        if guard:
            try:
                import ctypes as _ctypes
                _ctypes.WinDLL("kernel32").CloseHandle(guard)
            except Exception:
                pass
        # Final fallback
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
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
        except Exception:
            pass
    # On POSIX, pgid is just an int — nothing to release
