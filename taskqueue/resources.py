"""System resource detection and availability checking.

Run `tq detect` once per machine to inspect what the daemon will see.
"""

from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import psutil

from taskqueue.win_console import hide_console_check_output_kwargs

# Last successful GPU inventory for this process. A failed nvidia-smi must not
# be stored as "this machine has 0 GiB VRAM" (see apply_gpu_last_good).
_UNSET = object()
_nvidia_smi_resolved: Any = _UNSET
_gpu_last_good: dict[str, Any] | None = None

GPU_EMPTY_GRACE_SEC = 300.0
GPU_REFRESH_OK_SEC = 60.0
GPU_REFRESH_FAIL_BASE_SEC = 5.0

_NVIDIA_SMI_WIN_PATHS = (
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)


def reset_gpu_probe_state() -> None:
    """Drop cached nvidia-smi path and last-known-good inventory (tests)."""
    global _nvidia_smi_resolved, _gpu_last_good
    _nvidia_smi_resolved = _UNSET
    _gpu_last_good = None


def resolve_nvidia_smi() -> str | None:
    """Return an executable path for nvidia-smi, or None if it cannot be found.

    Searches PATH first, then well-known Windows driver locations. The result
    is cached for the process; call reset_gpu_probe_state() to clear.
    """
    global _nvidia_smi_resolved
    if _nvidia_smi_resolved is not _UNSET:
        return _nvidia_smi_resolved  # type: ignore[return-value]
    names = ("nvidia-smi.exe", "nvidia-smi") if os.name == "nt" else ("nvidia-smi",)
    seen: set[str] = set()
    for name in names:
        found = shutil.which(name)
        if found and found not in seen:
            _nvidia_smi_resolved = found
            return found
        if found:
            seen.add(found)
    if os.name == "nt":
        for candidate in _NVIDIA_SMI_WIN_PATHS:
            if os.path.isfile(candidate):
                _nvidia_smi_resolved = candidate
                return candidate
    _nvidia_smi_resolved = None
    return None


def gpu_refresh_interval(fail_streak: int) -> float:
    """Seconds until the next hardware refresh. Back off 5, 10, 20, cap 60."""
    if fail_streak <= 0:
        return GPU_REFRESH_OK_SEC
    return min(GPU_REFRESH_OK_SEC, GPU_REFRESH_FAIL_BASE_SEC * (2 ** (fail_streak - 1)))


@dataclass
class GpuProbeResult:
    gpus: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = False
    error: str | None = None
    nvidia_smi_path: str | None = None


def probe_gpus(*, timeout: float = 5.0) -> GpuProbeResult:
    """Run nvidia-smi and return a structured result (never raises).

    ``ok`` means the binary ran and we parsed stdout — including zero devices.
    ``ok=False`` is a probe failure (not found, timeout, nonzero exit), not
    "this machine has no GPU."
    """
    path = resolve_nvidia_smi()
    if not path:
        return GpuProbeResult(
            ok=False,
            error="nvidia-smi not found (no NVIDIA driver or not in PATH)",
        )
    try:
        proc = subprocess.run(
            [
                path,
                "--query-gpu=index,name,memory.total,memory.used,driver_version,"
                "temperature.gpu,utilization.gpu,power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ],
            timeout=timeout,
            capture_output=True,
            text=True,
            **hide_console_check_output_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return GpuProbeResult(
            ok=False,
            error=f"nvidia-smi timed out ({timeout:.0f}s)",
            nvidia_smi_path=path,
        )
    except FileNotFoundError:
        return GpuProbeResult(
            ok=False,
            error="nvidia-smi not found (no NVIDIA driver or not in PATH)",
            nvidia_smi_path=path,
        )
    except Exception as exc:
        return GpuProbeResult(
            ok=False,
            error=str(exc),
            nvidia_smi_path=path,
        )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        msg = f"nvidia-smi exited {proc.returncode}"
        if err:
            msg = f"{msg}: {err}"
        return GpuProbeResult(ok=False, error=msg, nvidia_smi_path=path)
    gpus: list[dict[str, Any]] = []
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            name = parts[1]
            total_mib = float(parts[2])
            used_mib = float(parts[3])
        except (TypeError, ValueError):
            continue
        entry: dict[str, Any] = {
            "index": idx,
            "name": name,
            "vram_gib": round(total_mib / 1024, 1),
            "vram_used_gib": round(used_mib / 1024, 1),
        }
        try:
            entry["driver_version"] = parts[4]
        except (IndexError, ValueError):
            pass
        try:
            entry["temperature_c"] = float(parts[5])
        except (IndexError, ValueError):
            pass
        try:
            entry["utilization_pct"] = float(parts[6])
        except (IndexError, ValueError):
            pass
        try:
            entry["power_draw_w"] = (
                float(parts[7]) if parts[7] not in ("[N/A]", "N/A", "") else None
            )
        except (IndexError, ValueError):
            pass
        try:
            entry["power_limit_w"] = (
                float(parts[8]) if parts[8] not in ("[N/A]", "N/A", "") else None
            )
        except (IndexError, ValueError):
            pass
        gpus.append(entry)
    return GpuProbeResult(gpus=gpus, ok=True, nvidia_smi_path=path)


@dataclass
class GpuProcessProbe:
    """Host-wide CUDA compute-apps. ``ok=False`` is a probe failure, not idle GPU."""

    processes: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = False
    error: str | None = None


_GENERIC_SIDECAR_HINTS = frozenset({"python", "node", "java"})
# Ollama's CUDA process on Windows is llama-server.exe, not ollama.exe.
_HINT_ALIASES: dict[str, tuple[str, ...]] = {
    "ollama": ("llama-server", "ollama_llama_server"),
}


def parse_json_obj(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if not val:
        return {}
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def parse_json_list(val: Any) -> list[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if not val:
        return []
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def task_requires_gpu(resources: Any) -> bool:
    """True when the task declares ``gpu_vram_gib > 0``."""
    res = parse_json_obj(resources)
    try:
        return float(res.get("gpu_vram_gib") or 0) > 0
    except (TypeError, ValueError):
        return False


def sidecar_name_hints(resources: Any, requires_services: Any = None) -> list[str]:
    """Lock names plus ``requires_services[].name`` used to attribute sidecar VRAM.

    Hints shorter than 3 characters and generic runtimes (python/node/java) are
    ignored so a lock of ``python`` does not match every CUDA Python process.
    """
    hints: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        s = str(raw or "").strip().lower()
        if len(s) < 3 or s in _GENERIC_SIDECAR_HINTS or s in seen:
            return
        seen.add(s)
        hints.append(s)

    res = parse_json_obj(resources)
    for lock in parse_json_list(res.get("locks")):
        _add(lock)
    for svc in parse_json_list(requires_services):
        if isinstance(svc, dict):
            _add(svc.get("name"))
        else:
            _add(svc)
    return hints


def attributed_gpu_pids(
    processes: list[dict[str, Any]] | None,
    tree_pids: Any,
    hints: list[str] | None,
) -> list[dict[str, Any]]:
    """Compute-apps in the task tree, or whose name/cmdline contains a sidecar hint."""
    tree: set[int] = set()
    for pid in tree_pids or []:
        try:
            tree.add(int(pid))
        except (TypeError, ValueError):
            continue
    hints_l: list[str] = []
    seen_h: set[str] = set()
    for h in hints or []:
        hl = str(h).lower()
        for token in (hl, *_HINT_ALIASES.get(hl, ())):
            if token and token not in seen_h:
                seen_h.add(token)
                hints_l.append(token)
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for proc in processes or []:
        try:
            pid = int(proc["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in seen:
            continue
        hay = " ".join(
            str(proc.get(k) or "").lower()
            for k in ("name", "cmdline_short", "smi_name")
        )
        if pid in tree or any(h in hay for h in hints_l):
            seen.add(pid)
            out.append(proc)
    return out


def attributed_vram_gib(
    processes: list[dict[str, Any]] | None,
    tree_pids: Any,
    hints: list[str] | None,
    *,
    device_used_gib: float | None = None,
) -> float:
    """Sum attributed compute-app VRAM.

    Windows WDDM often reports ``[N/A]`` per process. When attributed PIDs
    exist but none have a numeric VRAM, fall back to device ``memory.used``.
    """
    attributed = attributed_gpu_pids(processes, tree_pids, hints)
    total = 0.0
    unknown = False
    for proc in attributed:
        if proc.get("vram_unknown"):
            unknown = True
        try:
            total += float(proc.get("vram_gib") or 0)
        except (TypeError, ValueError):
            unknown = True
    if total > 0:
        return total
    if attributed and (unknown or device_used_gib is not None) and device_used_gib is not None:
        try:
            return max(0.0, float(device_used_gib))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def probe_gpu_memory_used(*, timeout: float = 2.0) -> float | None:
    """Sum nvidia-smi ``memory.used`` across devices, in GiB. None on probe failure."""
    path = resolve_nvidia_smi()
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            timeout=timeout,
            capture_output=True,
            text=True,
            **hide_console_check_output_kwargs(),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    total_mib = 0.0
    n = 0
    for line in (proc.stdout or "").strip().splitlines():
        raw = line.split(",")[0].strip()
        if raw in ("[N/A]", "N/A", ""):
            continue
        try:
            total_mib += float(raw)
            n += 1
        except (TypeError, ValueError):
            continue
    if not n:
        return None
    return round(total_mib / 1024.0, 3)


_SHARED_PROBE_TTL_SEC = 1.0
_shared_probe: dict[str, tuple[float, Any]] = {}


def shared_gpu_probe(kind: str, fn, ttl: float = _SHARED_PROBE_TTL_SEC):
    """Run ``fn()`` at most once per ``ttl`` seconds per ``kind``.

    Concurrent GPU runs each sample on every heartbeat; one nvidia-smi result serves them all.
    """
    now = time.monotonic()
    hit = _shared_probe.get(kind)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    value = fn()
    _shared_probe[kind] = (time.monotonic(), value)
    return value


def probe_gpu_compute_apps(*, timeout: float = 5.0) -> GpuProcessProbe:
    """Per-process CUDA VRAM from nvidia-smi compute-apps.

    ``ok`` means the binary ran; an empty ``processes`` list with ``ok=True``
    is a machine with no CUDA apps, not a probe failure.
    """
    path = resolve_nvidia_smi()
    if not path:
        return GpuProcessProbe(
            ok=False,
            error="nvidia-smi not found (no NVIDIA driver or not in PATH)",
        )
    try:
        proc = subprocess.run(
            [
                path,
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=timeout,
            capture_output=True,
            text=True,
            **hide_console_check_output_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return GpuProcessProbe(
            ok=False,
            error=f"nvidia-smi compute-apps timed out ({timeout:.0f}s)",
        )
    except FileNotFoundError:
        return GpuProcessProbe(
            ok=False,
            error="nvidia-smi not found (no NVIDIA driver or not in PATH)",
        )
    except Exception as exc:
        return GpuProcessProbe(ok=False, error=str(exc))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        msg = f"nvidia-smi compute-apps exited {proc.returncode}"
        if err:
            msg = f"{msg}: {err}"
        return GpuProcessProbe(ok=False, error=msg)
    rows: list[dict[str, Any]] = []
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except (TypeError, ValueError):
            continue
        mem_raw = parts[-1]
        smi_name = ",".join(parts[1:-1]).strip() if len(parts) > 2 else ""
        vram_unknown = mem_raw in ("[N/A]", "N/A", "")
        used_mib = 0.0
        if not vram_unknown:
            try:
                used_mib = float(mem_raw)
            except (TypeError, ValueError):
                vram_unknown = True
        meta = _gpu_proc_meta(pid)
        base = smi_name.replace("/", "\\").rsplit("\\", 1)[-1] if smi_name else ""
        name = meta["name"] or base or smi_name or f"pid:{pid}"
        rows.append({
            "pid": pid,
            "name": name,
            "smi_name": smi_name,
            "cmdline_short": meta["cmdline_short"] or smi_name or name,
            "vram_gib": round(used_mib / 1024.0, 3) if not vram_unknown else 0.0,
            "vram_unknown": vram_unknown,
        })
    rows.sort(key=lambda r: r["vram_gib"], reverse=True)
    return GpuProcessProbe(processes=rows, ok=True)


def probe_gpu_processes(*, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Host-wide CUDA apps (empty list on failure or none running)."""
    return probe_gpu_compute_apps(timeout=timeout).processes


def _gpu_proc_meta(pid: int) -> dict[str, str]:
    try:
        proc = psutil.Process(pid)
        name = proc.name() or ""
        cmd = proc.cmdline() or []
        cmdline_short = " ".join(str(c) for c in cmd)[-120:] if cmd else name
        return {"name": name, "cmdline_short": cmdline_short}
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {"name": "", "cmdline_short": ""}
    except Exception:
        return {"name": "", "cmdline_short": ""}


def apply_gpu_last_good(
    live: dict[str, Any],
    last_good: dict[str, Any] | None,
    *,
    now: float,
    empty_grace_sec: float = GPU_EMPTY_GRACE_SEC,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """Keep last-known-good GPU inventory when a probe fails or briefly goes empty.

    Returns ``(live, new_last_good, probe_unhealthy)``. Mutates *live* in place.
    ``probe_unhealthy`` is True when the caller should retry sooner than the
    healthy 60s cadence.
    """
    probe_ok = bool(live.get("gpu_probe_ok", True))
    gpus = list(live.get("gpus") or [])

    if probe_ok and gpus:
        live["gpu_probe_stale"] = False
        live["gpu_probe_error"] = None
        live["gpu_last_good_age_sec"] = 0.0
        new_good = {
            "gpus": [dict(g) for g in gpus],
            "gpu_vram_total_gib": float(live.get("gpu_vram_total_gib") or 0.0),
            "gpu_name": live.get("gpu_name"),
            "seen_at": now,
        }
        return live, new_good, False

    def _overlay(src: dict[str, Any]) -> None:
        live["gpus"] = [dict(g) for g in src["gpus"]]
        live["gpu_vram_total_gib"] = float(src.get("gpu_vram_total_gib") or 0.0)
        live["gpu_name"] = src.get("gpu_name")
        live["gpu_probe_stale"] = True
        live["gpu_last_good_age_sec"] = round(max(0.0, now - float(src.get("seen_at") or now)), 1)

    if not probe_ok:
        err = live.get("gpu_probe_error") or "nvidia-smi failed"
        live["gpu_probe_error"] = err
        if last_good and last_good.get("gpus"):
            _overlay(last_good)
            return live, last_good, True
        live["gpus"] = []
        live["gpu_vram_total_gib"] = 0.0
        live["gpu_name"] = None
        live["gpu_probe_stale"] = False
        live["gpu_last_good_age_sec"] = 0.0
        return live, last_good, True

    # Probe succeeded with zero devices.
    if last_good and last_good.get("gpus"):
        age = now - float(last_good.get("seen_at") or now)
        if age < empty_grace_sec:
            live["gpu_probe_error"] = live.get("gpu_probe_error") or (
                "nvidia-smi reported 0 devices"
            )
            _overlay(last_good)
            return live, last_good, True
        live["gpu_probe_stale"] = False
        live["gpu_probe_error"] = None
        live["gpu_last_good_age_sec"] = 0.0
        return live, None, False

    live["gpu_probe_stale"] = False
    live["gpu_last_good_age_sec"] = 0.0
    return live, last_good, False


_SYS_CACHE: tuple[float, dict] = (0.0, {})
_SYS_CACHE_TTL = 5.0
_SYS_CACHE_LOCK = threading.Lock()
_SYS_INFLIGHT: dict[str, threading.Event] = {}


def detect_system(*, apply_last_good: bool = True) -> dict[str, Any]:
    """Detect hardware capabilities. Saves nothing — caller decides where to persist.

    Cached for _SYS_CACHE_TTL seconds to avoid re-running nvidia-smi on every
    progress_snapshot or build_capacity call. Concurrent callers share one probe
    (single-flight) so a stampede after TTL expiry does not spawn N nvidia-smi.
    """
    global _SYS_CACHE
    _cached_at, _cached = _SYS_CACHE
    # Raw (apply_last_good=False) probes bypass the shared cache in both directions so a
    # failed probe is never served to callers that expect last-good smoothing.
    if apply_last_good and _cached and time.monotonic() - _cached_at < _SYS_CACHE_TTL:
        return dict(_cached)

    flight_key = "smooth" if apply_last_good else "raw"
    leader = False
    with _SYS_CACHE_LOCK:
        _cached_at, _cached = _SYS_CACHE
        if apply_last_good and _cached and time.monotonic() - _cached_at < _SYS_CACHE_TTL:
            return dict(_cached)
        ev = _SYS_INFLIGHT.get(flight_key)
        if ev is None:
            ev = threading.Event()
            _SYS_INFLIGHT[flight_key] = ev
            leader = True
    if not leader:
        ev.wait(timeout=30.0)
        _cached_at, _cached = _SYS_CACHE
        if _cached:
            return dict(_cached)

    try:
        return _detect_system_uncached(apply_last_good=apply_last_good)
    finally:
        with _SYS_CACHE_LOCK:
            done = _SYS_INFLIGHT.pop(flight_key, None)
            if done is not None:
                done.set()


def _detect_system_uncached(*, apply_last_good: bool = True) -> dict[str, Any]:
    """Body of :func:`detect_system` (caller holds single-flight leadership)."""
    global _SYS_CACHE

    cpu_count = psutil.cpu_count(logical=True) or 1
    cpu_phys = psutil.cpu_count(logical=False) or cpu_count
    vm = psutil.virtual_memory()
    ram_total_gib = vm.total / (1024 ** 3)
    ram_used_gib = vm.used / (1024 ** 3)
    probe = probe_gpus()
    gpus = probe.gpus
    total_vram = round(sum(g.get("vram_gib", 0.0) for g in gpus), 1)

    # CPU model name
    cpu_model: str | None = None
    try:
        import platform as _platform
        cpu_model = _platform.processor() or None
        if not cpu_model:
            import cpuinfo as _ci  # type: ignore[import]
            cpu_model = _ci.get_cpu_info().get("brand_raw")
    except Exception:
        pass

    # CPU frequency
    cpu_freq_mhz: float | None = None
    try:
        freq = psutil.cpu_freq()
        if freq:
            cpu_freq_mhz = round(freq.current, 0)
    except Exception:
        pass

    # Disk info (main drive)
    disks: list[dict[str, Any]] = []
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "mountpoint": part.mountpoint,
                    "device": part.device,
                    "fstype": part.fstype,
                    "total_gib": round(usage.total / (1024 ** 3), 1),
                    "used_gib": round(usage.used / (1024 ** 3), 1),
                    "free_gib": round(usage.free / (1024 ** 3), 1),
                    "pct_used": round(usage.percent, 1),
                })
            except Exception:
                pass
    except Exception:
        pass

    # OS info
    os_info: dict[str, Any] = {}
    try:
        import platform as _plat
        os_info = {
            "system": _plat.system(),
            "release": _plat.release(),
            "version": _plat.version(),
            "machine": _plat.machine(),
            "node": _plat.node(),
        }
    except Exception:
        pass

    live: dict[str, Any] = {
        "cpu_total_cores": cpu_count,
        "cpu_physical_cores": cpu_phys,
        "cpu_model": cpu_model,
        "cpu_freq_mhz": cpu_freq_mhz,
        "ram_total_gib": round(ram_total_gib, 1),
        "ram_used_gib": round(ram_used_gib, 1),
        "ram_available_gib": round(vm.available / (1024 ** 3), 1),
        "gpu_name": gpus[0]["name"] if gpus else None,
        "gpu_vram_total_gib": total_vram,
        "gpus": gpus,
        "disks": disks,
        "os": os_info,
        "gpu_probe_ok": probe.ok,
        "gpu_probe_error": probe.error,
        "gpu_probe_stale": False,
        "gpu_last_good_age_sec": 0.0,
        "nvidia_smi_path": probe.nvidia_smi_path,
        "gpu_processes": probe_gpu_processes() if probe.ok else [],
    }
    if apply_last_good:
        global _gpu_last_good
        live, _gpu_last_good, _unhealthy = apply_gpu_last_good(
            live, _gpu_last_good, now=time.monotonic()
        )
        live["gpu_probe_unhealthy"] = _unhealthy
    else:
        live["gpu_probe_unhealthy"] = not probe.ok
    if apply_last_good:
        _SYS_CACHE = (time.monotonic(), live)
    return dict(live)


def ram_used_gib() -> float:
    """Current system RAM used in GiB.

    Raises RuntimeError if psutil cannot read memory. Callers must treat that
    as "no headroom" (fail closed) rather than "no usage" — see
    ResourceBudget.measurement_failed.
    """
    try:
        return psutil.virtual_memory().used / (1024 ** 3)
    except Exception as exc:  # pragma: no cover - platform dependent
        raise RuntimeError(f"cannot read host RAM: {exc}") from exc


def top_ram_consumers(exclude_pids: set[int] | None = None, n: int = 8, *, budget_sec: float | None = None,
                      stats: dict | None = None) -> list[dict[str, Any]]:
    """Return the top-N processes by RSS, excluding tq-owned PIDs.

    Used by `tq doctor` to identify stray processes that block admission.
    Each entry: {pid, name, rss_gib, cmdline_short}. ``budget_sec`` stops the scan early
    (protected system processes make psutil slow on Windows); ``stats["partial"]`` then says so.
    """
    import time as _t

    deadline = _t.monotonic() + budget_sec if budget_sec else None
    exclude = exclude_pids or set()
    rows: list[tuple[float, Any]] = []
    try:
        # Pass 1: memory only (cheap). Command lines are expensive on Windows: fetch them for the top N only.
        for proc in psutil.process_iter(["pid", "memory_info"]):
            if deadline is not None and _t.monotonic() > deadline:
                if stats is not None:
                    stats["partial"] = True
                break
            try:
                info = proc.info
                if int(info["pid"]) in exclude or not info["memory_info"]:
                    continue
                rss = info["memory_info"].rss / (1024 ** 3)
                if rss >= 0.1:
                    rows.append((rss, proc))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    rows.sort(key=lambda t: t[0], reverse=True)
    out: list[dict[str, Any]] = []
    for rss, proc in rows[:n]:
        try:
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = ""
        try:
            cmd = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            cmd = []
        cmdline_short = " ".join(str(c) for c in cmd)[-120:] if cmd else name
        out.append({"pid": proc.pid, "name": name, "rss_gib": round(rss, 2), "cmdline_short": cmdline_short})
    return out


def cuda_health(*, check_torch: bool = False) -> dict[str, Any]:
    """Check CUDA/GPU health. Importing torch takes tens of seconds, so it is opt-in (``check_torch``).

    Returns a dict with keys:
      nvidia_smi_ok: bool           — nvidia-smi responded
      gpus: list[dict]              — per-device {index, name, vram_total_gib,
                                      vram_used_gib, vram_free_gib, temp_c,
                                      util_pct, driver}
      torch_cuda_available: bool|None  — None if torch not importable
      torch_device_name: str|None
      error: str|None               — first error encountered
    """
    result: dict[str, Any] = {
        "nvidia_smi_ok": False,
        "gpus": [],
        "torch_cuda_available": None,
        "torch_device_name": None,
        "error": None,
    }

    probe = probe_gpus()
    result["nvidia_smi_ok"] = probe.ok
    result["error"] = probe.error
    result["nvidia_smi_path"] = probe.nvidia_smi_path
    for g in probe.gpus:
        total = float(g.get("vram_gib") or 0.0)
        used = float(g.get("vram_used_gib") or 0.0)
        gpu: dict[str, Any] = {
            "index": g.get("index", 0),
            "name": g.get("name"),
            "vram_total_gib": total,
            "vram_used_gib": used,
            "vram_free_gib": round(max(0.0, total - used), 2),
        }
        if "temperature_c" in g:
            gpu["temp_c"] = g["temperature_c"]
        if "utilization_pct" in g:
            gpu["util_pct"] = g["utilization_pct"]
        if "driver_version" in g:
            gpu["driver"] = g["driver_version"]
        result["gpus"].append(gpu)

    # torch check (opt-in — slow to import, and may not be installed in the tq venv)
    if not check_torch:
        return result
    try:
        import importlib
        torch = importlib.import_module("torch")
        avail = bool(torch.cuda.is_available())
        result["torch_cuda_available"] = avail
        if avail:
            result["torch_device_name"] = torch.cuda.get_device_name(0)
    except ModuleNotFoundError:
        pass  # torch not present — leave None
    except Exception as exc:
        result["torch_cuda_available"] = False
        result["error"] = result["error"] or str(exc)

    return result


def _detect_gpus() -> list[dict[str, Any]]:
    """Return per-device GPU info from nvidia-smi (empty if the probe fails).

    Prefer ``probe_gpus()`` when the caller needs to distinguish "no GPU"
    from "nvidia-smi failed." This wrapper exists for display-only callers.
    """
    return probe_gpus().gpus


def gpu_used_vram_gib() -> float:
    """Current GPU VRAM used in GiB across all devices (0.0 if no GPU)."""
    gpus = _detect_gpus()
    return round(sum(float(g.get("vram_used_gib", 0.0)) for g in gpus), 2)


def pack_gpu(gpus_free: list[dict[str, Any]], request_vram: float) -> int | None:
    """First-fit: return device index that can hold request_vram, or None."""
    if request_vram <= 0:
        return None
    for g in sorted(gpus_free, key=lambda x: int(x.get("index", 0))):
        free = float(g.get("vram_gib_free", g.get("vram_gib", 0.0)))
        if free + 1e-9 >= request_vram:
            return int(g["index"])
    return None


def remote_worker_gpu_fits(gpus: list[dict[str, Any]], request_vram: float) -> bool:
    """Per-device first-fit check for a remote worker's self-reported GPU inventory.

    `gpus` entries may come straight off the wire as {index, name, vram_gib,
    vram_used_gib} (worker registration payload) rather than the master's own
    {index, vram_gib_free} shape — normalize here so a request that exceeds
    every single device's free VRAM is rejected even if the *pooled* total
    across devices would technically cover it (e.g. two 12GiB cards != one
    24GiB card).
    """
    if request_vram <= 0:
        return True
    normalized = [
        {
            "index": g.get("index", i),
            "vram_gib_free": g.get(
                "vram_gib_free",
                float(g.get("vram_gib", 0.0)) - float(g.get("vram_used_gib", 0.0)),
            ),
        }
        for i, g in enumerate(gpus)
    ]
    return pack_gpu(normalized, request_vram) is not None


def sort_queued_fairshare(
    queued: list[Any],
    gpu_running_by_proj: dict[str, int],
    usage_by_proj: dict[str, float] | None = None,
    fairshare_weight: float = 1e-6,
) -> list[Any]:
    """Higher priority first, then decayed GPU usage (lower = higher priority),
    then fewer currently running GPU tasks, then age.

    E4: Effective priority includes a starvation-prevention age term so
    lower-priority tasks don't wait forever behind high-priority projects.
    """
    usage = usage_by_proj or {}
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)

    def _sort_key(r):
        try:
            base_priority = int(r["priority"])
        except (TypeError, ValueError):
            base_priority = 0
        # Aging: +1 effective priority per 5 minutes waiting, capped at +20.
        enqueued = r["enqueued_at"] or ""
        age_bonus = 0.0
        if enqueued:
            try:
                enq = _parse_iso(enqueued)
                if enq.tzinfo is None:
                    enq = enq.replace(tzinfo=timezone.utc)
                waiting_sec = (now_utc - enq).total_seconds()
                age_bonus = float(min(20, int(waiting_sec // 300)))
            except Exception:
                pass
        eff_priority = base_priority + age_bonus
        return (
            -eff_priority,
            usage.get(r["project"], 0.0) * fairshare_weight,
            gpu_running_by_proj.get(r["project"], 0),
            enqueued,
        )

    return sorted(queued, key=_sort_key)


def cpu_cores_used_by_pids(pids: list[int]) -> int:
    """Estimate cores consumed by a list of PIDs (sum of cpu_affinity lengths)."""
    total = 0
    for pid in pids:
        try:
            p = psutil.Process(pid)
            aff = p.cpu_affinity()
            total += len(aff) if aff else 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            total += 1
    return total


def cpu_cores_from_load() -> float:
    """Host CPU usage expressed as cores-equivalent (non-blocking).

    Uses psutil.cpu_percent(percpu=True) which returns the delta since the last
    call on the same process — the first call in a new process always returns 0.0.
    Callers that need a meaningful reading should maintain an EMA across calls
    rather than using a single sample. Fail-open: any exception returns 0.0.
    """
    try:
        pcts = psutil.cpu_percent(percpu=True)
        return sum(pcts) / 100.0  # e.g. 4 cores at 50% each → 2.0 cores
    except Exception:
        return 0.0


class ResourceBudget:
    """Tracks available resources given config limits and currently running tasks."""

    def __init__(
        self,
        system: dict[str, Any],
        cfg: dict[str, Any],
    ) -> None:
        self.cpu_total: float = system.get("cpu_total_cores", 1)
        self.gpu_total: float = system.get("gpu_vram_total_gib", 0.0)
        self.ram_total: float = system.get("ram_total_gib", 0.0)
        self.gpus: list[dict[str, Any]] = list(system.get("gpus") or [])
        self.gpu_probe_error: str = str(system.get("gpu_probe_error") or "")
        self.gpu_probe_stale: bool = bool(system.get("gpu_probe_stale"))
        self.gpu_last_good_age_sec: float = float(system.get("gpu_last_good_age_sec") or 0.0)

        self.cpu_limit: float = self.cpu_total * cfg.get("cpu_limit_pct", 50) / 100.0
        self.gpu_limit: float = self.gpu_total * cfg.get("gpu_limit_pct", 100) / 100.0
        vram_cap = float(cfg.get("gpu_vram_limit_gib", 0) or 0)
        if vram_cap > 0:
            self.gpu_limit = min(self.gpu_limit, vram_cap)
        self.ram_limit: float = self.ram_total * cfg.get("ram_limit_pct", 80) / 100.0
        hard_pct = float(cfg.get("ram_hard_limit_pct", 95))
        self.ram_hard_limit: float = self.ram_total * hard_pct / 100.0
        if self.ram_hard_limit < self.ram_limit:
            self.ram_hard_limit = self.ram_limit

        # Seed RAM and GPU from live system usage so the aggregate gates account
        # for processes outside this scheduler's own reservations (OS, other
        # GPU users).
        # F-137 / G2: CPU gate is max(EMA, sum(declared)). Seeding EMA then also
        # adding every running task's declared cores double-counted and under-admitted.
        self.cpu_ema: float = float(system.get("cpu_used_cores_ema", 0.0))
        self.cpu_declared: float = 0.0
        self.cpu_reserved: float = self.cpu_ema
        self.gpu_live: float = round(
            sum(float(g.get("vram_used_gib", 0.0)) for g in self.gpus), 2
        )
        self.gpu_reserved: float = self.gpu_live
        # D10: fail closed. If we cannot measure host RAM we must not assume
        # zero usage — that would admit everything at the worst possible time.
        self.measurement_failed: str = ""
        try:
            self.ram_live: float = ram_used_gib()
            self.ram_reserved: float = self.ram_live
        except RuntimeError as exc:
            self.measurement_failed = str(exc)
            self.ram_live = self.ram_total
            self.ram_reserved = self.ram_total  # no headroom
        # Per-device free VRAM (declared reservations subtract first-fit).
        self.device_free: dict[int, float] = {}
        for g in self.gpus:
            idx = int(g.get("index", 0))
            total = float(g.get("vram_gib", 0.0))
            used = float(g.get("vram_used_gib", 0.0))
            self.device_free[idx] = max(0.0, total - used)

    def reserve(self, cpu_cores: float, gpu_vram_gib: float, ram_gib: float) -> None:
        self.cpu_declared += cpu_cores
        self.cpu_reserved = max(self.cpu_ema, self.cpu_declared)
        self.gpu_reserved += gpu_vram_gib
        self.ram_reserved += ram_gib
        if gpu_vram_gib > 0 and self.device_free:
            idx = pack_gpu(
                [{"index": i, "vram_gib_free": f} for i, f in self.device_free.items()],
                gpu_vram_gib,
            )
            if idx is not None:
                self.device_free[idx] = max(0.0, self.device_free[idx] - gpu_vram_gib)

    def fits(
        self,
        cpu_cores: float,
        gpu_vram_gib: float,
        ram_gib: float,
        *,
        ram_hold_credit: float = 0.0,
        gpu_hold_credit: float = 0.0,
    ) -> tuple[bool, str]:
        """Can a task of this estimated size start now?

        All three arguments are *estimates* (see estimate.estimate_task_resources),
        not raw declared values — an undeclared task must never be free (F2).

        ``ram_hold_credit`` / ``gpu_hold_credit`` exclude a P6 headroom reservation
        already attributed to this candidate so block_reason does not show
        ``hold + est`` for the same task footprint.
        """
        if self.measurement_failed:
            return False, f"resource measurement unavailable: {self.measurement_failed}"
        # F-137: gate on max(EMA, declared+candidate), not EMA+declared+candidate.
        cpu_need = max(self.cpu_ema, self.cpu_declared + cpu_cores)
        if cpu_need > self.cpu_limit:
            return False, (
                f"CPU: need {cpu_need:.1f} cores "
                f"but limit is {self.cpu_limit:.1f}"
            )
        if gpu_vram_gib > 0 and self.gpu_total > 0 and gpu_vram_gib > self.gpu_total:
            return False, (
                f"GPU: task requests {gpu_vram_gib:.1f} GiB but system total is "
                f"{self.gpu_total:.1f} GiB — this task can never run on this machine"
            )
        if gpu_vram_gib > 0 and self.gpu_limit <= 0 and self.gpu_probe_error:
            return False, f"GPU: probe failed: {self.gpu_probe_error}"
        gpu_credit = max(0.0, float(gpu_hold_credit))
        eff_gpu_reserved = self.gpu_reserved - gpu_credit
        if gpu_vram_gib > 0 and eff_gpu_reserved + gpu_vram_gib > self.gpu_limit:
            extra = ""
            if self.gpu_probe_error:
                extra = f" [{self.gpu_probe_error}"
                if self.gpu_probe_stale:
                    extra += (
                        f"; last_good={self.gpu_total:.1f} GiB "
                        f"stale={self.gpu_last_good_age_sec:.0f}s"
                    )
                extra += "]"
            hold = max(0.0, eff_gpu_reserved - self.gpu_live)
            if hold > 0.05:
                breakdown = (
                    f"live {self.gpu_live:.1f} + hold {hold:.1f} + est {gpu_vram_gib:.1f}"
                )
            else:
                breakdown = f"live {self.gpu_live:.1f} + est {gpu_vram_gib:.1f}"
            return False, (
                f"GPU: need {eff_gpu_reserved + gpu_vram_gib:.1f} GiB "
                f"({breakdown}) "
                f"but limit is {self.gpu_limit:.1f}{extra}"
            )
        if gpu_vram_gib > 0 and self.device_free:
            if pack_gpu(
                [{"index": i, "vram_gib_free": f} for i, f in self.device_free.items()],
                gpu_vram_gib,
            ) is None:
                return False, f"GPU: no device has {gpu_vram_gib:.1f} GiB free"
        # D2: additive host RAM gate. Previously this asked "am I already over?"
        # rather than "will this task put me over?", so admission always
        # overshot by at least one task (F1). ram_gib here is the *estimate*
        # (declared / predicted / floor), never a raw undeclared zero.
        credit = max(0.0, float(ram_hold_credit))
        eff_reserved = self.ram_reserved - credit
        if eff_reserved + ram_gib > self.ram_limit:
            # ram_reserved may include P6 headroom / same-tick holds that are
            # not physical RSS — spell that out so operators do not think the
            # host is somehow using more RAM than it has installed.
            hold = max(0.0, eff_reserved - self.ram_live)
            if hold > 0.05:
                breakdown = (
                    f"live {self.ram_live:.1f} + hold {hold:.1f} + est {ram_gib:.1f}"
                )
            else:
                breakdown = f"live {self.ram_live:.1f} + est {ram_gib:.1f}"
            return False, (
                f"RAM: need {eff_reserved + ram_gib:.1f} GiB "
                f"({breakdown}) "
                f"but soft limit is {self.ram_limit:.1f} "
                f"(total {self.ram_total:.1f}, hard {self.ram_hard_limit:.1f})"
            )
        return True, ""

    def antiflap_ram(
        self, hint_ram_gib: float, last_peak_ram_gib: float
    ) -> tuple[bool, str]:
        """Return (blocked, reason) if starting would likely hit the hard limit again.

        Uses live host RSS only — not ``ram_reserved``, which may include P6
        headroom holds and same-tick admission bookkeeping that are not yet
        allocated (see docs: live_used + max(hint, last_peak) > hard).
        """
        est = max(float(hint_ram_gib or 0.0), float(last_peak_ram_gib or 0.0))
        if est <= 0:
            return False, ""
        if self.ram_live + est > self.ram_hard_limit:
            return True, (
                f"RAM: prior peak/hint {est:.1f} GiB would exceed hard limit again "
                f"(live {self.ram_live:.1f} + est > hard {self.ram_hard_limit:.1f}; "
                f"total {self.ram_total:.1f})"
            )
        return False, ""

    def gpus_free_snapshot(self) -> list[dict[str, Any]]:
        out = []
        for g in self.gpus:
            idx = int(g.get("index", 0))
            total = float(g.get("vram_gib", 0.0))
            free = self.device_free.get(idx, total)
            out.append({
                "index": idx,
                "name": g.get("name"),
                "vram_gib_total": total,
                "vram_gib_free": round(free, 2),
            })
        return out
