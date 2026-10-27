"""Windows console-hiding helpers for nested task spawns."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from taskqueue.win_console import apply_hide_console_popen_kwargs, inject_python_hide_console


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_inject_pythonpath_sitecustomize() -> None:
    env = inject_python_hide_console({})
    assert "PYTHONPATH" in env
    assert (Path(env["PYTHONPATH"].split(os.pathsep)[0]) / "sitecustomize.py").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_apply_hide_console_sets_flags() -> None:
    kw = apply_hide_console_popen_kwargs({"args": [sys.executable, "-c", "pass"]})
    assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kw["startupinfo"].wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_sitecustomize_patches_subprocess() -> None:
    """Fresh interpreter with injected PYTHONPATH should wrap subprocess.Popen."""
    env = inject_python_hide_console(os.environ.copy())
    code = (
        "import subprocess\n"
        "print(getattr(subprocess.Popen, '__name__', type(subprocess.Popen).__name__))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr
    # Accept either the old function name or the new class name — both indicate patching worked.
    assert proc.stdout.strip() in ("_popen_no_window", "_PopenNoWindow"), proc.stdout
