"""GPU probe failure vs empty inventory, last-known-good, Ollama VRAM timeouts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from taskqueue import errors as tqerr
from taskqueue.resources import (
    ResourceBudget,
    apply_gpu_last_good,
    gpu_refresh_interval,
    probe_gpus,
    reset_gpu_probe_state,
    resolve_nvidia_smi,
)
from taskqueue.scheduler import _check_services, _warm_vram_admit_gib


@pytest.fixture(autouse=True)
def _reset_gpu_probe_cache() -> None:
    reset_gpu_probe_state()
    yield
    reset_gpu_probe_state()


def _gpu(index: int = 0, *, vram: float = 8.0, used: float = 1.0, name: str = "FakeGPU") -> dict:
    return {"index": index, "name": name, "vram_gib": vram, "vram_used_gib": used}


def _last_good(*, now: float = 100.0) -> dict:
    return {
        "gpus": [_gpu()],
        "gpu_vram_total_gib": 8.0,
        "gpu_name": "FakeGPU",
        "seen_at": now,
    }


def test_gpu_refresh_interval_backoff() -> None:
    assert gpu_refresh_interval(0) == 60.0
    assert gpu_refresh_interval(1) == 5.0
    assert gpu_refresh_interval(2) == 10.0
    assert gpu_refresh_interval(3) == 20.0
    assert gpu_refresh_interval(5) == 60.0


def test_probe_gpus_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        "taskqueue.resources.resolve_nvidia_smi", lambda: r"C:\fake\nvidia-smi.exe"
    )

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)

    monkeypatch.setattr("taskqueue.resources.subprocess.run", _timeout)
    result = probe_gpus()
    assert result.ok is False
    assert result.gpus == []
    assert "timed out" in (result.error or "")


def test_probe_gpus_not_found(monkeypatch) -> None:
    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: None)
    result = probe_gpus()
    assert result.ok is False
    assert "not found" in (result.error or "")


def test_probe_gpus_parses_csv(monkeypatch) -> None:
    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: "/bin/nvidia-smi")

    class _Proc:
        returncode = 0
        stdout = "0, NVIDIA GeForce RTX 4060, 8188, 1024, 560.00, 45, 3, 20.0, 115.0\n"
        stderr = ""

    monkeypatch.setattr("taskqueue.resources.subprocess.run", lambda *_a, **_k: _Proc())
    result = probe_gpus()
    assert result.ok is True
    assert result.error is None
    assert len(result.gpus) == 1
    assert result.gpus[0]["name"] == "NVIDIA GeForce RTX 4060"
    assert result.gpus[0]["vram_gib"] == pytest.approx(8.0, abs=0.1)


def test_resolve_nvidia_smi_windows_fallback(monkeypatch) -> None:
    monkeypatch.setattr("taskqueue.resources.os.name", "nt")
    monkeypatch.setattr("taskqueue.resources.shutil.which", lambda _n: None)

    def _isfile(path: str) -> bool:
        return path == r"C:\Windows\System32\nvidia-smi.exe"

    monkeypatch.setattr("taskqueue.resources.os.path.isfile", _isfile)
    assert resolve_nvidia_smi() == r"C:\Windows\System32\nvidia-smi.exe"


def test_last_good_kept_on_timeout() -> None:
    live = {
        "gpu_probe_ok": False,
        "gpu_probe_error": "nvidia-smi timed out (5s)",
        "gpus": [],
        "gpu_vram_total_gib": 0.0,
        "gpu_name": None,
    }
    out, new_good, unhealthy = apply_gpu_last_good(live, _last_good(), now=141.0)
    assert unhealthy is True
    assert new_good is not None
    assert out["gpu_vram_total_gib"] == 8.0
    assert out["gpu_probe_stale"] is True
    assert out["gpus"][0]["name"] == "FakeGPU"
    assert "timed out" in (out["gpu_probe_error"] or "")
    assert out["gpu_last_good_age_sec"] == pytest.approx(41.0)


def test_never_seen_gpu_stays_fail_closed() -> None:
    live = {
        "gpu_probe_ok": False,
        "gpu_probe_error": "nvidia-smi not found (no NVIDIA driver or not in PATH)",
        "gpus": [],
        "gpu_vram_total_gib": 0.0,
        "gpu_name": None,
    }
    out, new_good, unhealthy = apply_gpu_last_good(live, None, now=10.0)
    assert unhealthy is True
    assert new_good is None
    assert out["gpu_vram_total_gib"] == 0.0
    assert out["gpu_probe_stale"] is False
    assert "not found" in (out["gpu_probe_error"] or "")


def test_empty_devices_kept_during_grace() -> None:
    live = {
        "gpu_probe_ok": True,
        "gpu_probe_error": None,
        "gpus": [],
        "gpu_vram_total_gib": 0.0,
        "gpu_name": None,
    }
    out, new_good, unhealthy = apply_gpu_last_good(
        live, _last_good(now=100.0), now=110.0, empty_grace_sec=300.0
    )
    assert unhealthy is True
    assert new_good is not None
    assert out["gpu_vram_total_gib"] == 8.0
    assert out["gpu_probe_stale"] is True


def test_empty_devices_accepted_after_grace() -> None:
    live = {
        "gpu_probe_ok": True,
        "gpu_probe_error": None,
        "gpus": [],
        "gpu_vram_total_gib": 0.0,
        "gpu_name": None,
    }
    out, new_good, unhealthy = apply_gpu_last_good(
        live, _last_good(now=100.0), now=500.0, empty_grace_sec=300.0
    )
    assert unhealthy is False
    assert new_good is None
    assert out["gpu_vram_total_gib"] == 0.0
    assert out["gpu_probe_stale"] is False


def test_success_clears_stale() -> None:
    live = {
        "gpu_probe_ok": True,
        "gpu_probe_error": None,
        "gpus": [_gpu(used=2.0)],
        "gpu_vram_total_gib": 8.0,
        "gpu_name": "FakeGPU",
    }
    out, new_good, unhealthy = apply_gpu_last_good(live, _last_good(), now=200.0)
    assert unhealthy is False
    assert out["gpu_probe_stale"] is False
    assert out["gpu_probe_error"] is None
    assert new_good is not None
    assert new_good["seen_at"] == 200.0
    assert new_good["gpus"][0]["vram_used_gib"] == 2.0


def _budget(system: dict, monkeypatch) -> ResourceBudget:
    import taskqueue.resources as R

    monkeypatch.setattr(R, "ram_used_gib", lambda: 1.0)
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 80}
    b = ResourceBudget(system, cfg)
    b.ram_reserved = 1.0
    b.ram_live = 1.0
    return b


def test_fits_probe_failed_no_inventory_is_explicit(monkeypatch) -> None:
    b = _budget(
        {
            "cpu_total_cores": 8,
            "ram_total_gib": 64.0,
            "gpu_vram_total_gib": 0.0,
            "gpus": [],
            "gpu_probe_error": "nvidia-smi timed out (5s)",
        },
        monkeypatch,
    )
    ok, reason = b.fits(1, 4.3, 1.0)
    assert ok is False
    assert "probe failed" in reason
    assert "timed out" in reason
    assert "limit is 0.0" not in reason


def test_fits_last_good_still_admits(monkeypatch) -> None:
    b = _budget(
        {
            "cpu_total_cores": 8,
            "ram_total_gib": 64.0,
            "gpu_vram_total_gib": 8.0,
            "gpus": [_gpu(used=1.0)],
            "gpu_probe_error": "nvidia-smi timed out (5s)",
            "gpu_probe_stale": True,
            "gpu_last_good_age_sec": 12.0,
        },
        monkeypatch,
    )
    ok, _reason = b.fits(1, 4.3, 1.0)
    assert ok is True


def test_capacity_exposes_probe_error(tmp_path: Path, monkeypatch) -> None:
    from taskqueue.capacity import build_capacity
    from taskqueue.db import open_db

    monkeypatch.setattr(
        "taskqueue.capacity.detect_system",
        lambda: {
            "cpu_total_cores": 8,
            "ram_total_gib": 32.0,
            "ram_used_gib": 4.0,
            "gpu_name": None,
            "gpu_vram_total_gib": 0.0,
            "gpus": [],
            "gpu_probe_error": "nvidia-smi timed out (5s)",
            "gpu_probe_stale": False,
            "gpu_last_good_age_sec": 0.0,
            "gpu_processes": [],
        },
    )
    monkeypatch.setattr("taskqueue.capacity.ram_used_gib", lambda: 4.0)
    db = open_db(tmp_path / "q.db")
    cap = build_capacity(db, {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100})
    assert cap["master"]["gpu_probe_error"] == "nvidia-smi timed out (5s)"
    assert cap["master"]["gpus"] == []
    assert cap["master"]["gpu_processes"] == []


# --- Ollama / requires_services VRAM probe --------------------------------


class _HttpResp:
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _HttpResp:
        return self

    def __exit__(self, *_a) -> bool:
        return False


def _ollama_yaml(**extra) -> str:
    svc = {
        "name": "ollama",
        "health_url": "http://localhost:11434/api/tags",
        "timeout_sec": 5,
        "min_vram_free_gib": 4.3,
    }
    svc.update(extra)
    return json.dumps([svc])


def _url_of(req) -> str:
    return getattr(req, "full_url", None) or getattr(req, "get_full_url", lambda: str(req))()


def test_vram_probe_timeout_is_not_service_vram(monkeypatch) -> None:
    def _urlopen(req, timeout=None):
        url = _url_of(req)
        if "/api/ps" in url:
            raise TimeoutError("timed out")
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(_ollama_yaml())
    assert code == tqerr.BLOCK_SERVICE
    assert "timed out" in reason
    assert "retrying" in reason


def test_vram_probe_timeout_does_not_call_detect_system(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("detect_system must not run on the admit path")

    monkeypatch.setattr("taskqueue.resources.detect_system", _boom)

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            raise TimeoutError("timed out")
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(_ollama_yaml())
    assert code == tqerr.BLOCK_SERVICE
    assert "timed out" in reason


def test_vram_shortfall_still_service_vram(monkeypatch) -> None:
    body = json.dumps({"models": [{"size_vram": int(6 * 1024**3)}]}).encode()

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            return _HttpResp(body)
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(
        _ollama_yaml(),
        system_info={"gpu_vram_total_gib": 8.0, "gpus": [], "gpu_probe_stale": False},
    )
    assert code == tqerr.BLOCK_SERVICE_VRAM
    assert "VRAM not ready" in reason


def test_service_vram_uses_smi_free_when_fresh(monkeypatch) -> None:
    """Gate + message follow nvidia-smi free, not Ollama total−resident accounting."""
    body = json.dumps(
        {"models": [{"name": "llama3.2:latest", "size_vram": int(2.4 * 1024**3)}]}
    ).encode()

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            return _HttpResp(body)
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(
        _ollama_yaml(min_vram_free_gib=4.0),
        system_info={
            "gpu_vram_total_gib": 6.0,
            "gpu_probe_stale": False,
            "gpus": [_gpu(vram=6.0, used=2.0)],  # 4.0 GiB free on smi
        },
    )
    assert reason is None
    assert code == ""

    reason2, code2 = _check_services(
        _ollama_yaml(min_vram_free_gib=4.5),
        system_info={
            "gpu_vram_total_gib": 6.0,
            "gpu_probe_stale": False,
            "gpus": [_gpu(vram=6.0, used=2.0)],
        },
    )
    assert code2 == tqerr.BLOCK_SERVICE_VRAM
    assert "4.0 GiB free" in reason2
    assert "in use: 2.0 GiB" in reason2


def test_vram_probe_skipped_when_device_has_headroom(monkeypatch) -> None:
    calls: list[str] = []

    def _urlopen(req, timeout=None):
        url = _url_of(req)
        calls.append(url)
        if "/api/ps" in url:
            raise AssertionError("/api/ps should be skipped when nvidia-smi shows headroom")
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(
        _ollama_yaml(),
        system_info={
            "gpu_vram_total_gib": 8.0,
            "gpu_probe_stale": False,
            "gpus": [_gpu(used=1.0)],
        },
    )
    assert reason is None
    assert code == ""
    assert calls and all("/api/ps" not in u for u in calls)


def test_resident_ok_models_passes_when_pinned_model_loaded(monkeypatch) -> None:
    """Loaded llama3.2 with low free VRAM should pass when it is the only resident model."""
    body = json.dumps(
        {
            "models": [
                {
                    "name": "llama3.2:latest",
                    "model": "llama3.2:latest",
                    "size_vram": int(2.4 * 1024**3),
                }
            ]
        }
    ).encode()

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            return _HttpResp(body)
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(
        _ollama_yaml(resident_ok_models=["llama3.2"]),
        system_info={
            "gpu_vram_total_gib": 6.0,
            "gpu_probe_stale": True,
            "gpus": [_gpu(vram=6.0, used=2.5)],
        },
    )
    assert reason is None
    assert code == ""


def test_resident_ok_models_blocks_wrong_model(monkeypatch) -> None:
    body = json.dumps(
        {
            "models": [
                {
                    "name": "llama3.1:latest",
                    "model": "llama3.1:latest",
                    "size_vram": int(5.0 * 1024**3),
                }
            ]
        }
    ).encode()

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            return _HttpResp(body)
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(
        _ollama_yaml(resident_ok_models=["llama3.2"]),
        system_info={"gpu_vram_total_gib": 8.0, "gpus": [], "gpu_probe_stale": False},
    )
    assert code == tqerr.BLOCK_SERVICE_VRAM
    assert "VRAM not ready" in reason


def test_resident_ok_models_blocks_mixed_residents(monkeypatch) -> None:
    body = json.dumps(
        {
            "models": [
                {
                    "name": "llama3.2:latest",
                    "model": "llama3.2:latest",
                    "size_vram": int(2.4 * 1024**3),
                },
                {
                    "name": "qwen2.5:7b",
                    "model": "qwen2.5:7b",
                    "size_vram": int(4.0 * 1024**3),
                },
            ]
        }
    ).encode()

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            return _HttpResp(body)
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    reason, code = _check_services(
        _ollama_yaml(resident_ok_models=["llama3.2"]),
        system_info={"gpu_vram_total_gib": 8.0, "gpus": [], "gpu_probe_stale": False},
    )
    assert code == tqerr.BLOCK_SERVICE_VRAM


def test_warm_vram_admit_gib_zero_when_resident_ok() -> None:
    ps = [{"name": "llama3.2:latest", "model": "llama3.2:latest", "size_vram": 1}]
    svc = _ollama_yaml(resident_ok_models=["llama3.2"])
    assert _warm_vram_admit_gib(4.3, svc, ps) == 0.0


def test_warm_vram_admit_gib_keeps_estimate_when_wrong_model() -> None:
    ps = [{"name": "llama3.1:latest", "model": "llama3.1:latest", "size_vram": 1}]
    svc = _ollama_yaml(resident_ok_models=["llama3.2"])
    assert _warm_vram_admit_gib(4.3, svc, ps) == 4.3


def test_vram_probe_uses_system_info_total_not_detect_system(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("detect_system must not run on the admit path")

    monkeypatch.setattr("taskqueue.resources.detect_system", _boom)
    body = json.dumps({"models": []}).encode()

    def _urlopen(req, timeout=None):
        if "/api/ps" in _url_of(req):
            return _HttpResp(body)
        return _HttpResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    # Stale snapshot so we do not skip /api/ps; total still comes from system_info.
    reason, code = _check_services(
        _ollama_yaml(),
        system_info={
            "gpu_vram_total_gib": 8.0,
            "gpu_probe_stale": True,
            "gpus": [_gpu(used=7.5)],
        },
    )
    assert reason is None
    assert code == ""


def test_probe_gpu_processes_parses_and_sorts(monkeypatch) -> None:
    from taskqueue.resources import probe_gpu_processes

    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: "/bin/nvidia-smi")

    class _Proc:
        returncode = 0
        stdout = "111, ollama.exe, 4300\n222, python.exe, 512\n"
        stderr = ""

    monkeypatch.setattr("taskqueue.resources.subprocess.run", lambda *_a, **_k: _Proc())
    monkeypatch.setattr(
        "taskqueue.resources._gpu_proc_meta",
        lambda pid: {"name": "", "cmdline_short": ""},
    )
    rows = probe_gpu_processes()
    assert [r["pid"] for r in rows] == [111, 222]
    assert rows[0]["name"] == "ollama.exe"
    assert rows[0]["vram_gib"] == pytest.approx(4.199, abs=0.01)
    assert rows[0].get("vram_unknown") is False


def test_probe_gpu_processes_keeps_wddm_na(monkeypatch) -> None:
    from taskqueue.resources import probe_gpu_processes

    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: "/bin/nvidia-smi")

    class _Proc:
        returncode = 0
        stdout = (
            r"9792, C:\Users\example\AppData\Local\Programs\Ollama\lib\ollama\llama-server.exe, [N/A]"
            "\n"
        )
        stderr = ""

    monkeypatch.setattr("taskqueue.resources.subprocess.run", lambda *_a, **_k: _Proc())
    monkeypatch.setattr(
        "taskqueue.resources._gpu_proc_meta",
        lambda pid: {"name": "", "cmdline_short": ""},
    )
    rows = probe_gpu_processes()
    assert len(rows) == 1
    assert rows[0]["pid"] == 9792
    assert rows[0]["name"] == "llama-server.exe"
    assert rows[0]["vram_gib"] == 0.0
    assert rows[0]["vram_unknown"] is True
    assert "ollama" in rows[0]["smi_name"].lower()


def test_probe_gpu_memory_used(monkeypatch) -> None:
    from taskqueue.resources import probe_gpu_memory_used

    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: "/bin/nvidia-smi")

    class _Proc:
        returncode = 0
        stdout = "5535\n"
        stderr = ""

    monkeypatch.setattr("taskqueue.resources.subprocess.run", lambda *_a, **_k: _Proc())
    assert probe_gpu_memory_used() == pytest.approx(5.405, abs=0.01)


def test_probe_gpu_compute_apps_ok_empty(monkeypatch) -> None:
    from taskqueue.resources import probe_gpu_compute_apps

    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: "/bin/nvidia-smi")

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("taskqueue.resources.subprocess.run", lambda *_a, **_k: _Proc())
    result = probe_gpu_compute_apps()
    assert result.ok is True
    assert result.processes == []


def test_probe_gpu_compute_apps_failure_not_empty_success(monkeypatch) -> None:
    from taskqueue.resources import probe_gpu_compute_apps, probe_gpu_processes

    monkeypatch.setattr("taskqueue.resources.resolve_nvidia_smi", lambda: None)
    result = probe_gpu_compute_apps()
    assert result.ok is False
    assert result.processes == []
    assert result.error
    assert probe_gpu_processes() == []


def test_capacity_includes_gpu_processes(tmp_path: Path, monkeypatch) -> None:
    from taskqueue.capacity import build_capacity
    from taskqueue.db import open_db

    monkeypatch.setattr(
        "taskqueue.capacity.detect_system",
        lambda: {
            "cpu_total_cores": 8,
            "ram_total_gib": 32.0,
            "ram_used_gib": 4.0,
            "gpu_name": "FakeGPU",
            "gpu_vram_total_gib": 8.0,
            "gpus": [_gpu()],
            "gpu_processes": [
                {
                    "pid": 42,
                    "name": "ollama.exe",
                    "cmdline_short": "ollama serve",
                    "vram_gib": 4.3,
                },
            ],
        },
    )
    monkeypatch.setattr("taskqueue.capacity.ram_used_gib", lambda: 4.0)
    db = open_db(tmp_path / "q.db")
    cap = build_capacity(db, {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100})
    assert cap["master"]["gpu_processes"][0]["name"] == "ollama.exe"
