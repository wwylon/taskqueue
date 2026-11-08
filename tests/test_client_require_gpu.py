"""tq_client.require_gpu / env_info."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
import tq_client as tq  # noqa: E402


def test_env_info_reads_tq_vars(monkeypatch):
    monkeypatch.setenv("TQ_HOME", "/tmp/tq")
    monkeypatch.setenv("TQ_PROJECT", "p")
    monkeypatch.delenv("TQ_TASK_ID", raising=False)
    info = tq.env_info()
    assert info["TQ_HOME"] == "/tmp/tq"
    assert info["TQ_PROJECT"] == "p"
    assert info["TQ_TASK_ID"] is None


def test_require_gpu_uses_torch_when_available(monkeypatch):
    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", torch)
    tq.require_gpu()


def test_require_gpu_falls_back_to_nvidia_smi(monkeypatch):
    torch = types.ModuleType("torch")

    def _boom():
        raise RuntimeError("no cuda")

    torch.cuda = types.SimpleNamespace(is_available=_boom)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)

    class _CP:
        returncode = 0
        stdout = "GPU 0: Fake\n"
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda name: "nvidia-smi")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP())
    tq.require_gpu()


def test_require_gpu_exits_when_no_gpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    sys.modules["torch"].cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        is_available=lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        tq.require_gpu(exit_code=9)
    assert ei.value.code == 9
