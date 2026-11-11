"""The local ``taskqueue`` shim keeps old entry points working after the rename to tqdaemon."""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The taskqueue/ shim lives only in the private checkout; the public export (and wheel) do not ship it.
needs_shim = pytest.mark.skipif(not (ROOT / "taskqueue" / "__init__.py").is_file(), reason="legacy shim not present (public tree)")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=120)


@needs_shim
def test_old_imports_are_the_same_objects():
    code = (
        "import tqdaemon.db, tqdaemon.web.handler, taskqueue.db as a, taskqueue.web.handler as b, taskqueue.cli as c, "
        "tqdaemon.cli as d; from taskqueue import adopt; import tqdaemon.adopt as e;"
        "assert a is tqdaemon.db and b is tqdaemon.web.handler and c is d and adopt is e;"
        "import taskqueue; assert taskqueue.__version__ == tqdaemon.__version__"
    )
    r = _run("-c", code)
    assert r.returncode == 0, r.stderr


@needs_shim
def test_old_entry_point_target_works():
    r = _run("-c", "from taskqueue.cli import main; print(callable(main))")
    assert r.returncode == 0 and "True" in r.stdout, r.stderr


@needs_shim
def test_python_dash_m_old_names_still_run():
    r = _run("-m", "taskqueue.run_wrapper")
    assert "usage: python -m tqdaemon.run_wrapper" in (r.stderr + r.stdout)
    r = _run("-m", "taskqueue.cli", "--help")
    assert r.returncode == 0 and "tq" in (r.stdout + r.stderr)


def test_shim_is_not_shipped_in_the_wheel():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["tqdaemon"]
    assert set(cfg["project"]["scripts"]) <= {"tq", "tqdaemon"}
