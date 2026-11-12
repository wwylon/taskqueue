"""Hardware probe must keep working when NVML is missing or fails (non-NVIDIA hosts, Linux/macOS without drivers)."""

from __future__ import annotations

import sys

import pytest

from tqdaemon import resources
from tqdaemon.web import hardware


@pytest.fixture(autouse=True)
def _no_smi(monkeypatch):
    monkeypatch.setattr(resources, "_detect_gpus", lambda: [])


def test_nvml_library_missing(monkeypatch):
    pynvml = pytest.importorskip("pynvml")

    def boom():
        raise pynvml.NVMLError_LibraryNotFound()

    monkeypatch.setattr(pynvml, "nvmlInit", boom)
    data = hardware._get_hardware_data()
    assert data["gpu"] == [] and data["cpu_logical_cores"] >= 1
    assert "gpu_error" in data


def test_pynvml_not_importable(monkeypatch):
    monkeypatch.setitem(sys.modules, "pynvml", None)  # `import pynvml` raises ImportError
    data = hardware._get_hardware_data()
    assert data["gpu"] == [] and data["cpu_logical_cores"] >= 1
