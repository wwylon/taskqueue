"""Host sleep inhibit (Windows) — complements F-001 clock-jump reclaim."""

from __future__ import annotations

import os
import sys

_ES_CONTINUOUS        = 0x80000000
_ES_SYSTEM_REQUIRED   = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040  # prevents away-mode / connected-standby sleep
_inhibiting = False


def set_prevent_sleep(enable: bool) -> bool:
    """Request OS keep the system awake while True. Returns whether applied."""
    global _inhibiting
    if os.name != "nt":
        _inhibiting = False
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if enable:
            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
            kernel32.SetThreadExecutionState(flags)
            _inhibiting = True
            return True
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        _inhibiting = False
        return True
    except Exception as exc:
        print(f"[queue] warn: prevent_sleep failed: {exc}", file=sys.stderr)
        _inhibiting = False
        return False


