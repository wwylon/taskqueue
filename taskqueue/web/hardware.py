"""Hardware monitoring snapshot for /api/hardware."""
from __future__ import annotations

import threading
from typing import Any

_hw_cache: dict = {"ts": 0.0, "data": None}
_hw_lock = threading.Lock()

def gpu_fit_summary(hw: dict, vram_gib: float, ram_gib: float = 0.0) -> dict:
    """Which GPUs could host a task needing ``vram_gib`` (and ``ram_gib`` host RAM) right now."""
    gpus = []
    for i, g in enumerate(hw.get("gpu") or []):
        free = max(0.0, float(g.get("mem_total_gib") or 0) - float(g.get("mem_used_gib") or 0))
        gpus.append({
            "index": i, "name": g.get("name"), "free_gib": round(free, 2),
            "total_gib": g.get("mem_total_gib"), "fits": free >= vram_gib,
        })
    ram_free = hw.get("ram_available_gib")
    ram_ok = ram_free is None or float(ram_free) >= ram_gib
    fitting = [g["index"] for g in gpus if g["fits"]]
    return {
        "vram_gib": vram_gib, "ram_gib": ram_gib, "ram_ok": ram_ok,
        "gpus": gpus, "fits_now": ram_ok and (bool(fitting) or vram_gib <= 0),
        "best_gpu": min((g for g in gpus if g["fits"]), key=lambda g: g["free_gib"], default={}).get("index"),
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
        "cpu_temp_c": None, "cpu_freq_mhz": None, "gpu": [],
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
        result["ram_total_gib"] = round(vm.total / (1024 ** 3), 2)
        result["ram_used_gib"] = round(vm.used / (1024 ** 3), 2)
        result["ram_available_gib"] = round(vm.available / (1024 ** 3), 2)
        result["ram_pct"] = vm.percent
        try:
            sw = psutil.swap_memory()
            result["swap_total_gib"] = round(sw.total / (1024 ** 3), 2)
            result["swap_used_gib"] = round(sw.used / (1024 ** 3), 2)
            result["swap_pct"] = sw.percent
        except Exception:
            pass
        try:
            temps = psutil.sensors_temperatures()  # type: ignore[attr-defined]
            if temps:
                for _name, entries in temps.items():
                    if entries:
                        result["cpu_temp_c"] = round(entries[0].current, 1)
                        break
        except Exception:
            pass
    except ImportError:
        pass

    try:
        result["cpu_model"] = platform.processor() or None
    except Exception:
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
            except Exception:
                pass
            gpu_entry: dict[str, Any] = {
                "name": name,
                "load_pct": util.gpu,
                "mem_load_pct": util.memory,
                "mem_used_gib": round(mem.used / (1024 ** 3), 2),
                "mem_total_gib": round(mem.total / (1024 ** 3), 2),
                "temp_c": temp,
                "throttle_reasons": throttle_reasons,
            }
            try:
                gpu_entry["power_draw_w"] = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)
                gpu_entry["power_limit_w"] = round(pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0, 1)
            except Exception:
                pass
            try:
                gpu_entry["fan_speed_pct"] = pynvml.nvmlDeviceGetFanSpeed(handle)
            except Exception:
                pass
            try:
                gpu_entry["clock_graphics_mhz"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
                gpu_entry["clock_mem_mhz"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
                gpu_entry["clock_sm_mhz"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
                gpu_entry["clock_max_graphics_mhz"] = pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
                gpu_entry["clock_max_mem_mhz"] = pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_MEM)
            except Exception:
                pass
            try:
                pcie = pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)
                pcie_w = pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)
                gpu_entry["pcie_gen"] = pcie
                gpu_entry["pcie_width"] = pcie_w
            except Exception:
                pass
            try:
                cc = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                gpu_entry["compute_capability"] = f"{cc[0]}.{cc[1]}"
            except Exception:
                pass
            try:
                serial = pynvml.nvmlDeviceGetSerial(handle)
                if serial and isinstance(serial, bytes):
                    serial = serial.decode("utf-8", errors="replace")
                gpu_entry["driver_version"] = pynvml.nvmlSystemGetDriverVersion()
            except Exception:
                pass
            try:
                cuda_ver = pynvml.nvmlSystemGetCudaDriverVersion()
                gpu_entry["cuda_version"] = f"{cuda_ver // 1000}.{(cuda_ver % 1000) // 10}"
            except Exception:
                pass
            result["gpu"].append(gpu_entry)
    except ImportError:
        pass
    except Exception as e:
        result["gpu_error"] = str(e)
        print(f"[tq] GPU init failed: {e}", flush=True)

    if not result["gpu"]:
        # pynvml unavailable — fall back to nvidia-smi for basic GPU info
        from taskqueue.resources import _detect_gpus
        for g in _detect_gpus():
            result["gpu"].append({
                "name": g.get("name", "GPU"),
                "load_pct": 0,
                "mem_used_gib": g.get("vram_used_gib", 0.0),
                "mem_total_gib": g.get("vram_gib", 0.0),
                "throttle_reasons": [],
            })

    with _hw_lock:
        _hw_cache["ts"] = time.monotonic()
        _hw_cache["data"] = result

    return result

