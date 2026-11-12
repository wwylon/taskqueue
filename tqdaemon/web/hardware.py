"""Hardware monitoring snapshot for /api/hardware."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

_log = logging.getLogger(__name__)

_hw_cache: dict = {"ts": 0.0, "data": None}
_hw_lock = threading.Lock()


def gpu_fit_summary(hw: dict, vram_gib: float, ram_gib: float = 0.0) -> dict:
    """Which GPUs could host a task needing ``vram_gib`` (and ``ram_gib`` host RAM) right now."""
    gpus = []
    for i, g in enumerate(hw.get("gpu") or []):
        free = max(0.0, float(g.get("mem_total_gib") or 0) - float(g.get("mem_used_gib") or 0))
        gpus.append(
            {
                "index": i,
                "name": g.get("name"),
                "free_gib": round(free, 2),
                "total_gib": g.get("mem_total_gib"),
                "fits": free >= vram_gib,
            }
        )
    ram_free = hw.get("ram_available_gib")
    ram_ok = ram_free is None or float(ram_free) >= ram_gib
    fitting = [g["index"] for g in gpus if g["fits"]]
    return {
        "vram_gib": vram_gib,
        "ram_gib": ram_gib,
        "ram_ok": ram_ok,
        "gpus": gpus,
        "fits_now": ram_ok and (bool(fitting) or vram_gib <= 0),
        "best_gpu": min((g for g in gpus if g["fits"]), key=lambda g: g["free_gib"], default={}).get("index"),
    }


def _proc_info(pid: int) -> dict[str, Any]:
    """Name, short command line and whether the process belongs to a tqdaemon run."""
    info: dict[str, Any] = {"name": f"pid {pid}", "cmd": "", "tq": False}
    try:
        import psutil

        proc = psutil.Process(pid)
        with proc.oneshot():
            info["name"] = proc.name()
            try:
                info["cmd"] = " ".join(proc.cmdline())[:200]
            except (psutil.AccessDenied, psutil.ZombieProcess):
                pass
        me = os.getpid()
        try:
            for parent in proc.parents():
                if parent.pid == me:
                    info["tq"] = True
                    break
                try:
                    if "run_wrapper" in " ".join(parent.cmdline()):
                        info["tq"] = True
                        break
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass
    except Exception:  # noqa: BLE001, S110 - other users' / exited processes: keep the pid only
        pass
    return info


_WIN_COUNTER_TTL_SEC = 15.0
_win_counter_cache: dict[str, Any] = {"ts": 0.0, "data": {}}


def _windows_gpu_process_memory() -> dict[str, dict[int, int]]:
    """``{adapter_luid: {pid: dedicated_bytes}}`` from the Windows "GPU Process Memory"
    performance counters (the source Task Manager uses). Cached: one typeperf call ~1-2 s."""
    now = time.monotonic()
    if now - float(_win_counter_cache["ts"]) < _WIN_COUNTER_TTL_SEC:
        return _win_counter_cache["data"]
    import csv
    import io
    import re
    import subprocess

    out: dict[str, dict[int, int]] = {}
    try:
        proc = subprocess.run(
            ["typeperf", r"\GPU Process Memory(*)\Dedicated Usage", "-sc", "1"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = [r for r in csv.reader(io.StringIO(proc.stdout)) if r]
        if len(rows) >= 2:
            header, values = rows[0], rows[1]
            pat = re.compile(r"pid_(\d+)_luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", re.IGNORECASE)
            for name, val in zip(header[1:], values[1:], strict=False):
                m = pat.search(name)
                if not m:
                    continue
                try:
                    used = int(float(val))
                except ValueError:
                    continue
                luid = m.group(2).lower()
                pid = int(m.group(1))
                out.setdefault(luid, {})[pid] = out.get(luid, {}).get(pid, 0) + used
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("GPU process counters unavailable: %s", exc)
    _win_counter_cache["ts"] = now
    _win_counter_cache["data"] = out
    return out


def _fill_from_windows_counters(by_pid: dict[int, dict[str, Any]], mem_used_gib: float) -> None:
    """WDDM: NVML reports no per-process memory. Use the adapter whose counter total is closest
    to this device's used memory, fill in the known processes and add programs NVML missed."""
    adapters = _windows_gpu_process_memory()
    if not adapters:
        return
    target = float(mem_used_gib or 0.0) * (1024**3)
    luid = min(adapters, key=lambda k: abs(sum(adapters[k].values()) - target))
    for pid, used in adapters[luid].items():
        if used <= 0 or pid <= 4:  # pid 0/4: idle/System kernel allocations
            continue
        entry = by_pid.setdefault(pid, {"pid": pid, "kinds": ["wddm"], "vram_gib": None})
        entry["vram_gib"] = round(used / (1024**3), 2)


def gpu_processes_for_device(pynvml: Any, handle: Any, mem_used_gib: float) -> dict[str, Any]:
    """Which programs hold VRAM on one device: compute (CUDA) and graphics (browsers, games,
    desktop) processes from NVML. ``vram_gib`` is None when the driver does not report it
    (Windows WDDM usually does not); ``other_gib`` is device memory not attributed to any
    listed process."""
    by_pid: dict[int, dict[str, Any]] = {}
    for kind, fn_name in (
        ("compute", "nvmlDeviceGetComputeRunningProcesses"),
        ("graphics", "nvmlDeviceGetGraphicsRunningProcesses"),
    ):
        fn = getattr(pynvml, fn_name, None)
        if fn is None:
            continue
        try:
            procs = fn(handle) or []
        except Exception:  # noqa: BLE001 - NVMLError / not supported on this driver
            continue
        for p in procs:
            pid = int(getattr(p, "pid", 0) or 0)
            if pid <= 0:
                continue
            used = getattr(p, "usedGpuMemory", None)
            gib = round(used / (1024**3), 2) if isinstance(used, int) and used >= 0 else None
            entry = by_pid.setdefault(pid, {"pid": pid, "kinds": [], "vram_gib": None})
            if kind not in entry["kinds"]:
                entry["kinds"].append(kind)
            if gib is not None:
                entry["vram_gib"] = max(gib, entry["vram_gib"] or 0.0)
    if os.name == "nt" and (not by_pid or any(e["vram_gib"] is None for e in by_pid.values())):
        _fill_from_windows_counters(by_pid, mem_used_gib)
    rows = []
    for entry in by_pid.values():
        entry.update(_proc_info(entry["pid"]))
        rows.append(entry)
    rows.sort(key=lambda r: (r["vram_gib"] is None, -(r["vram_gib"] or 0.0), r["name"]))
    known = sum(r["vram_gib"] or 0.0 for r in rows)
    unknown = sum(1 for r in rows if r["vram_gib"] is None)
    return {
        "list": rows,
        "attributed_gib": round(known, 2),
        "other_gib": round(max(0.0, float(mem_used_gib or 0.0) - known), 2),
        "per_process_unknown": unknown,
    }


def _get_hardware_data() -> dict:
    """Return cached hardware snapshot; refresh if older than 5 seconds."""
    import time

    now = time.monotonic()
    with _hw_lock:
        if now - _hw_cache["ts"] < 5.0 and _hw_cache["data"] is not None:
            return _hw_cache["data"]

    import platform

    result: dict[str, Any] = {
        "cpu_temp_c": None,
        "cpu_freq_mhz": None,
        "gpu": [],
        "os": platform.system() + " " + platform.release(),
        "arch": platform.machine(),
    }

    try:
        import psutil

        freq = psutil.cpu_freq()
        if freq:
            result["cpu_freq_mhz"] = round(freq.current)
            result["cpu_freq_max_mhz"] = round(freq.max) if freq.max else None
        result["cpu_physical_cores"] = psutil.cpu_count(logical=False) or 1
        result["cpu_logical_cores"] = psutil.cpu_count(logical=True) or 1
        # F-049: call cpu_percent once with percpu=True then derive total from it
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        result["cpu_util_pct"] = round(sum(per_cpu) / len(per_cpu), 1) if per_cpu else 0.0
        result["cpu_per_core_pct"] = per_cpu if isinstance(per_cpu, list) else []
        vm = psutil.virtual_memory()
        result["ram_total_gib"] = round(vm.total / (1024**3), 2)
        result["ram_used_gib"] = round(vm.used / (1024**3), 2)
        result["ram_available_gib"] = round(vm.available / (1024**3), 2)
        result["ram_pct"] = vm.percent
        try:
            sw = psutil.swap_memory()
            result["swap_total_gib"] = round(sw.total / (1024**3), 2)
            result["swap_used_gib"] = round(sw.used / (1024**3), 2)
            result["swap_pct"] = sw.percent
        except (OSError, AttributeError):
            pass
        try:
            temps = psutil.sensors_temperatures()  # type: ignore[attr-defined]
            if temps:
                for entries in temps.values():
                    if entries:
                        result["cpu_temp_c"] = round(entries[0].current, 1)
                        break
        except (OSError, AttributeError):
            pass
    except ImportError:
        pass

    try:
        result["cpu_model"] = platform.processor() or None
    except OSError:
        pass

    try:
        import pynvml  # type: ignore[import]

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        _THROTTLE = {
            0x2: "power_cap",
            0x4: "thermal",
            0x8: "sync_boost",
            0x10: "board_power_limit",
            0x20: "low_util",
            0x40: "reliability",
            0x100: "board_limit",
            0x200: "idle",
            0x400: "unknown",
        }
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            throttle_reasons: list[str] = []
            try:
                mask = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(handle)
                for bit, label in _THROTTLE.items():
                    if mask & bit:
                        throttle_reasons.append(label)
            except (pynvml.NVMLError, AttributeError):
                pass
            gpu_entry: dict[str, Any] = {
                "name": name,
                "load_pct": util.gpu,
                "mem_load_pct": util.memory,
                "mem_used_gib": round(mem.used / (1024**3), 2),
                "mem_total_gib": round(mem.total / (1024**3), 2),
                "temp_c": temp,
                "throttle_reasons": throttle_reasons,
            }
            try:
                gpu_entry["power_draw_w"] = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)
                gpu_entry["power_limit_w"] = round(pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0, 1)
            except (pynvml.NVMLError, AttributeError):
                pass
            try:
                gpu_entry["fan_speed_pct"] = pynvml.nvmlDeviceGetFanSpeed(handle)
            except (pynvml.NVMLError, AttributeError):
                pass
            try:
                gpu_entry["clock_graphics_mhz"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
                gpu_entry["clock_mem_mhz"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
                gpu_entry["clock_sm_mhz"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
                gpu_entry["clock_max_graphics_mhz"] = pynvml.nvmlDeviceGetMaxClockInfo(
                    handle, pynvml.NVML_CLOCK_GRAPHICS
                )
                gpu_entry["clock_max_mem_mhz"] = pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_MEM)
            except (pynvml.NVMLError, AttributeError):
                pass
            try:
                pcie = pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)
                pcie_w = pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)
                gpu_entry["pcie_gen"] = pcie
                gpu_entry["pcie_width"] = pcie_w
            except (pynvml.NVMLError, AttributeError):
                pass
            try:
                cc = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                gpu_entry["compute_capability"] = f"{cc[0]}.{cc[1]}"
            except (pynvml.NVMLError, AttributeError):
                pass
            try:
                serial = pynvml.nvmlDeviceGetSerial(handle)
                if serial and isinstance(serial, bytes):
                    serial = serial.decode("utf-8", errors="replace")
                gpu_entry["driver_version"] = pynvml.nvmlSystemGetDriverVersion()
            except (pynvml.NVMLError, AttributeError):
                pass
            try:
                cuda_ver = pynvml.nvmlSystemGetCudaDriverVersion()
                gpu_entry["cuda_version"] = f"{cuda_ver // 1000}.{(cuda_ver % 1000) // 10}"
            except (pynvml.NVMLError, AttributeError):
                pass
            gpu_entry["processes"] = gpu_processes_for_device(pynvml, handle, gpu_entry["mem_used_gib"])
            result["gpu"].append(gpu_entry)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - NVML init fails in many ways
        result["gpu_error"] = str(e)
        print(f"[tq] GPU init failed: {e}", flush=True)

    if not result["gpu"]:
        # pynvml unavailable — fall back to nvidia-smi for basic GPU info
        from tqdaemon.resources import _detect_gpus

        for g in _detect_gpus():
            result["gpu"].append(
                {
                    "name": g.get("name", "GPU"),
                    "load_pct": 0,
                    "mem_used_gib": g.get("vram_used_gib", 0.0),
                    "mem_total_gib": g.get("vram_gib", 0.0),
                    "throttle_reasons": [],
                }
            )

    with _hw_lock:
        _hw_cache["ts"] = time.monotonic()
        _hw_cache["data"] = result

    return result
