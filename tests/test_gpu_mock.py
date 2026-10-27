"""Mocked GPU counts for capacity + packing."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue.capacity import build_capacity
from taskqueue.db import open_db
from taskqueue.resources import (
    ResourceBudget,
    pack_gpu,
    remote_worker_gpu_fits,
    sort_queued_fairshare,
)


def _fake_detect(n: int):
    def _detect():
        gpus = [
            {"index": i, "name": f"FakeGPU{i}", "vram_gib": 8.0, "vram_used_gib": 0.0}
            for i in range(n)
        ]
        return {
            "cpu_total_cores": 8,
            "ram_total_gib": 32.0,
            "ram_used_gib": 4.0,
            "gpu_name": gpus[0]["name"] if gpus else None,
            "gpu_vram_total_gib": 8.0 * n,
            "gpus": gpus,
        }

    return _detect


@pytest.mark.parametrize("n", [0, 1, 3])
def test_capacity_gpu_count(tmp_path: Path, monkeypatch, n: int) -> None:
    monkeypatch.setattr("taskqueue.capacity.detect_system", _fake_detect(n))
    monkeypatch.setattr("taskqueue.capacity.ram_used_gib", lambda: 4.0)
    db = open_db(tmp_path / "q.db")
    cap = build_capacity(db, {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100})
    assert len(cap["master"]["gpus"]) == n
    if n:
        assert sum(g["vram_gib_total"] for g in cap["master"]["gpus"]) == pytest.approx(8.0 * n)


def test_pack_gpu_first_fit() -> None:
    free = [
        {"index": 0, "vram_gib_free": 2.0},
        {"index": 1, "vram_gib_free": 8.0},
        {"index": 2, "vram_gib_free": 8.0},
    ]
    assert pack_gpu(free, 6.0) == 1
    assert pack_gpu(free, 9.0) is None


def test_budget_seeds_gpu_reserved_from_live_usage() -> None:
    # Regression for bug #4: a single 8GiB card already has 6GiB used by
    # something outside this scheduler's own reservations (another process,
    # or a task the daemon isn't tracking). The aggregate gpu_reserved gate
    # must reflect that live usage from construction, mirroring how
    # ram_reserved is seeded from ram_used_gib() -- not start at 0 and only
    # grow from this scheduler's own reserve() calls.
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": 32.0,
        "gpu_vram_total_gib": 8.0,
        "gpus": [{"index": 0, "name": "GPU0", "vram_gib": 8.0, "vram_used_gib": 6.0}],
    }
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100}
    budget = ResourceBudget(system, cfg)
    budget.ram_reserved = 1.0  # isolate GPU assert from live host RAM
    assert budget.gpu_reserved == pytest.approx(6.0)
    # Only ~2GiB left in the aggregate gate even though nothing has reserve()'d yet.
    ok, _ = budget.fits(1, 3.0, 0)
    assert ok is False
    ok2, _ = budget.fits(1, 1.5, 0)
    assert ok2 is True


def test_budget_rejects_overask_across_devices() -> None:
    system = _fake_detect(3)()
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100}
    budget = ResourceBudget(system, cfg)
    budget.ram_reserved = 0.0
    # Three 8 GiB cards: 9 GiB request cannot pack
    ok, reason = budget.fits(1, 9.0, 0)
    assert ok is False
    assert "device" in reason.lower() or "gpu" in reason.lower()
    # 8 GiB fits on first device
    ok2, _ = budget.fits(1, 8.0, 0)
    assert ok2 is True


def test_fits_gpu_breakdown_live_plus_est() -> None:
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": 32.0,
        "gpu_vram_total_gib": 6.0,
        "gpus": [{"index": 0, "name": "GPU0", "vram_gib": 6.0, "vram_used_gib": 4.0}],
    }
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 100}
    budget = ResourceBudget(system, cfg)
    ok, reason = budget.fits(1, 5.0, 0.0)
    assert ok is False
    assert "9.0" in reason
    assert "live 4.0" in reason
    assert "est 5.0" in reason


def test_remote_worker_rejects_pooled_but_not_per_device_fit() -> None:
    # 2x12GiB worker, as self-reported on registration (wire shape, not
    # ResourceBudget's internal shape) — pooled total is 24GiB.
    gpus = [
        {"index": 0, "name": "GPU0", "vram_gib": 12.0, "vram_used_gib": 0.0},
        {"index": 1, "name": "GPU1", "vram_gib": 12.0, "vram_used_gib": 0.0},
    ]
    # Regression for bug #1: 20GiB fits the 24GiB pool but no single device.
    assert remote_worker_gpu_fits(gpus, 20.0) is False
    # 10GiB fits comfortably on one device.
    assert remote_worker_gpu_fits(gpus, 10.0) is True
    # Zero-GPU request always "fits" (no GPU needed).
    assert remote_worker_gpu_fits([], 0.0) is True
    # No GPU inventory but a GPU is requested: caller falls back to pooled
    # comparison for old workers; this helper alone can't satisfy it.
    assert remote_worker_gpu_fits([], 1.0) is False


def test_fairshare_prefers_lighter_gpu_project() -> None:
    class R(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    queued = [
        R(project="heavy", task_id="a", priority=50, enqueued_at="2020-01-01T00:00:00"),
        R(project="light", task_id="b", priority=50, enqueued_at="2020-01-01T00:01:00"),
    ]
    ordered = sort_queued_fairshare(queued, {"heavy": 2, "light": 0})
    assert ordered[0]["project"] == "light"
