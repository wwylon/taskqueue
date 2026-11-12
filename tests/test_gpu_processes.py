"""Per-program VRAM listing for the Resources page (NVML + Windows counter fallback)."""

from __future__ import annotations

from types import SimpleNamespace

from tqdaemon.web import hardware


class _FakeNvml:
    def __init__(self, compute, graphics):
        self._c, self._g = compute, graphics

    def nvmlDeviceGetComputeRunningProcesses(self, _h):
        return self._c

    def nvmlDeviceGetGraphicsRunningProcesses(self, _h):
        return self._g


def test_processes_merged_sorted_and_other_memory(monkeypatch):
    monkeypatch.setattr(hardware.os, "name", "posix")  # no Windows counter fallback
    monkeypatch.setattr(hardware, "_proc_info", lambda pid: {"name": f"p{pid}", "cmd": "", "tq": pid == 20})
    gib = 1024**3
    nv = _FakeNvml(
        [SimpleNamespace(pid=10, usedGpuMemory=2 * gib), SimpleNamespace(pid=20, usedGpuMemory=1 * gib)],
        [SimpleNamespace(pid=10, usedGpuMemory=None), SimpleNamespace(pid=30, usedGpuMemory=None)],
    )
    out = hardware.gpu_processes_for_device(nv, None, 4.0)
    assert [r["pid"] for r in out["list"]] == [10, 20, 30]
    assert out["list"][0]["kinds"] == ["compute", "graphics"] and out["list"][0]["vram_gib"] == 2.0
    assert out["list"][1]["tq"] is True
    assert out["attributed_gib"] == 3.0 and out["other_gib"] == 1.0 and out["per_process_unknown"] == 1


def test_windows_counters_fill_unknown_memory(monkeypatch):
    monkeypatch.setattr(hardware.os, "name", "nt")
    monkeypatch.setattr(hardware, "_proc_info", lambda pid: {"name": f"p{pid}", "cmd": "", "tq": False})
    gib = 1024**3
    monkeypatch.setattr(
        hardware,
        "_windows_gpu_process_memory",
        lambda: {"igpu": {50: 100}, "nvidia": {4: 5 * 1024**2, 10: int(1.5 * gib), 77: int(0.5 * gib)}},
    )
    nv = _FakeNvml([SimpleNamespace(pid=10, usedGpuMemory=None)], [])
    out = hardware.gpu_processes_for_device(nv, None, 2.0)
    by = {r["pid"]: r for r in out["list"]}
    assert by[10]["vram_gib"] == 1.5 and by[77]["vram_gib"] == 0.5  # 77: a program NVML did not list
    assert 4 not in by and 50 not in by  # System pid and the other adapter are skipped
    assert out["per_process_unknown"] == 0


def test_nvml_errors_give_an_empty_list(monkeypatch):
    monkeypatch.setattr(hardware.os, "name", "posix")

    class Broken:
        def nvmlDeviceGetComputeRunningProcesses(self, _h):
            raise RuntimeError("not supported")

    out = hardware.gpu_processes_for_device(Broken(), None, 1.0)
    assert out["list"] == [] and out["other_gib"] == 1.0
