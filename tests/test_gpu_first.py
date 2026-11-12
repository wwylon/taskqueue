"""GPU-first ordering: equal-priority GPU tasks go before CPU tasks while the GPU is idle."""

from __future__ import annotations

from tqdaemon.resources import sort_queued_fairshare


def _row(tid, priority=50, gpu=0.0, enq="2026-01-01T00:00:00+00:00"):
    return {
        "project": "p",
        "task_id": tid,
        "priority": priority,
        "enqueued_at": enq,
        "resources": '{"gpu_vram_gib": %s}' % gpu,
    }


def test_gpu_task_first_only_when_enabled():
    rows = [_row("cpu", enq="2026-01-01T00:00:00+00:00"), _row("gpu", gpu=4, enq="2026-01-01T00:00:01+00:00")]
    assert [r["task_id"] for r in sort_queued_fairshare(rows, {})] == ["cpu", "gpu"]
    assert [r["task_id"] for r in sort_queued_fairshare(rows, {}, gpu_first=True)] == ["gpu", "cpu"]


def test_priority_still_wins_over_gpu_first():
    rows = [_row("cpu-high", priority=90), _row("gpu-low", priority=10, gpu=4)]
    assert [r["task_id"] for r in sort_queued_fairshare(rows, {}, gpu_first=True)] == ["cpu-high", "gpu-low"]


def test_config_key_is_valid():
    from tqdaemon.config import validate_config

    assert validate_config("gpu_first", "0") is None
    assert validate_config("gpu_first", "2") is not None
