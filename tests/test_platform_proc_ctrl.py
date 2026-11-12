"""terminate_guarded must never broadcast CTRL_C_EVENT to the whole console (it interrupted pytest)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tqdaemon import platform_proc

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows console ctrl events")


def _spawn(**kw: object) -> tuple[subprocess.Popen, object]:
    return platform_proc.spawn_with_guard([sys.executable, "-c", "import time; time.sleep(30)"], **kw)


def test_no_ctrl_event_without_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GenerateConsoleCtrlEvent", lambda ev, pid: calls.append((ev, pid)) or 1
    )
    proc, guard = _spawn()
    platform_proc.terminate_guarded(proc, guard, graceful_sec=0.2)
    assert proc.wait(10) is not None
    assert calls == []


def test_ctrl_break_only_to_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GenerateConsoleCtrlEvent", lambda ev, pid: calls.append((ev, pid)) or 1
    )
    proc, guard = _spawn(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    platform_proc.terminate_guarded(proc, guard, graceful_sec=0.2)
    assert proc.wait(10) is not None
    assert calls == [(1, proc.pid)]
