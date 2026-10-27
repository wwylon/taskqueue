"""Auto-imported via PYTHONPATH when taskqueue starts a Windows task.

Console-subsystem grandchildren (e.g. go.exe spawned from a Python task script)
allocate a visible console unless CreateProcess gets CREATE_NO_WINDOW. The runner
already sets that flag on the root process; this patches subprocess so nested
spawns inherit the same behavior.
"""

from __future__ import annotations

import os
import subprocess

if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
    _real_popen = subprocess.Popen

    class _PopenNoWindow(_real_popen):  # type: ignore[misc]
        """subprocess.Popen subclass that forces CREATE_NO_WINDOW on every spawn.

        Must be a class (not a bare function) so that code which subclasses
        subprocess.Popen (e.g. asyncio.windows_utils) still works correctly.
        """

        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            flags = int(kwargs.get("creationflags") or 0)
            kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
            si = kwargs.get("startupinfo")
            if si is None:
                si = subprocess.STARTUPINFO()
                kwargs["startupinfo"] = si
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            super().__init__(*args, **kwargs)

    subprocess.Popen = _PopenNoWindow  # type: ignore[assignment]
