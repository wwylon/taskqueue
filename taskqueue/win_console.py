"""Windows helpers to keep task subprocess trees from flashing consoles."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _sitecustomize_dir() -> Path:
    return Path(__file__).resolve().parent / "_win_sitecustomize"


def inject_python_hide_console(env: dict[str, str]) -> dict[str, str]:
    """Prepend sitecustomize so nested Python subprocess.* calls hide consoles."""
    if os.name != "nt":
        return env
    hide = str(_sitecustomize_dir())
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = hide + (os.pathsep + prev if prev else "")
    return env


def hide_console_check_output_kwargs() -> dict:
    """Return kwargs for subprocess.check_output that suppress any console flash."""
    if os.name != "nt":
        return {}
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": flags, "startupinfo": si}


def apply_hide_console_popen_kwargs(popen_kwargs: dict) -> dict:
    """Add CREATE_NO_WINDOW + SW_HIDE startupinfo for the root task process."""
    if os.name != "nt":
        return popen_kwargs
    flags = int(popen_kwargs.get("creationflags") or 0)
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    popen_kwargs["creationflags"] = flags
    si = popen_kwargs.get("startupinfo")
    if si is None:
        si = subprocess.STARTUPINFO()
        popen_kwargs["startupinfo"] = si
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return popen_kwargs
