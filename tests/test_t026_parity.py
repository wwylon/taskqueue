"""T-026: local master and remote worker share Filter-Score-Reserve-Assign."""

from __future__ import annotations

import json

import pytest

from taskqueue.scheduler import (
    MASTER_NODE_ID,
    FilterContext,
    Node,
    filter_task,
    node_from_system,
    node_from_worker,
)
from taskqueue.placement import score_assignment


def _task(
    *,
    project="proj",
    task_id="T1",
    dispatch="any",
    exclusive=False,
    exclusive_project=False,
    resources=None,
    requires=None,
    depends_on=None,
    max_attempts=0,
):
    return {
        "project": project,
        "task_id": task_id,
        "dispatch": dispatch,
        "exclusive": exclusive,
        "exclusive_project": exclusive_project,
        "resources": json.dumps(resources or {"cpu_cores": 1, "ram_gib": 2.0, "gpu_vram_gib": 0.0}),
        "requires": json.dumps(requires or {}),
        "depends_on": json.dumps(depends_on or []),
        "max_attempts": max_attempts,
        "force_run": 0,
        "requires_services": "[]",
    }


def _twin_nodes(**kwargs) -> tuple[Node, Node]:
    base = dict(
        os="windows",
        arch="x86_64",
        ram_gib=16.0,
        cpu_cores=8.0,
        gpu_vram_gib=12.0,
        gpus=[{"index": 0, "vram_gib": 12.0, "vram_used_gib": 0.0}],
        tags=["lab"],
        ram_used_gib=0.0,
        vram_used_gib=0.0,
        cpu_used=0.0,
    )
    base.update(kwargs)
    local = Node(node_id=MASTER_NODE_ID, is_local=True, projects=None, **base)
    remote = Node(node_id="w1", is_local=False, projects=["proj"], **base)
    return local, remote


def _ctx(**kwargs) -> FilterContext:
    defaults = dict(
        n_running=0,
        has_exclusive=False,
        online_workers=1,
        dep_status=lambda p, t: None,
    )
    defaults.update(kwargs)
    return FilterContext(**defaults)


def _decide(row, node, ctx, *, cpu=1.0, ram=2.0, gpu=0.0, attempts=0):
    return filter_task(row, node, ctx, cpu=cpu, ram=ram, gpu=gpu, attempt_count=attempts)


CASES = [
    ("admit_fit", _task(), _ctx(), True),
    ("paused", _task(), _ctx(paused=True), False),
    ("frozen", _task(), _ctx(frozen=True), False),
    ("exclusive_blocked", _task(exclusive=True), _ctx(n_running=1), False),
    ("exclusive_running", _task(), _ctx(has_exclusive=True), False),
    (
        "exclusive_project_blocked",
        _task(exclusive_project=True),
        _ctx(n_running_by_proj={"proj": 1}),
        False,
    ),
    (
        "exclusive_project_running",
        _task(),
        _ctx(exclusive_projects={"proj": "proj/ex"}, n_running_by_proj={"proj": 1}),
        False,
    ),
    ("global_max_tasks", _task(), _ctx(n_running=1, g_max_tasks=1), False),
    ("project_max_tasks", _task(), _ctx(n_running_by_proj={"proj": 1}, project_limits={"max_tasks": 1}), False),
    (
        "deps_unmet",
        _task(depends_on=["dep"]),
        _ctx(dep_status=lambda p, t: "queued"),
        False,
    ),
    (
        "deps_met",
        _task(depends_on=["dep"]),
        _ctx(dep_status=lambda p, t: "completed"),
        True,
    ),
    (
        "os_mismatch",
        _task(requires={"os": "linux"}),
        _ctx(),
        False,
    ),
    (
        "ram_over_node",
        _task(resources={"cpu_cores": 1, "ram_gib": 32.0, "gpu_vram_gib": 0.0}),
        _ctx(),
        False,
    ),
    (
        "gpu_over_card",
        _task(resources={"cpu_cores": 1, "ram_gib": 1.0, "gpu_vram_gib": 24.0}),
        _ctx(),
        False,
    ),
]


@pytest.mark.parametrize("name,row,ctx,expect_ok", CASES, ids=[c[0] for c in CASES])
def test_t026_local_remote_same_decision(name, row, ctx, expect_ok):
    local, remote = _twin_nodes()
    cpu, ram, gpu = 1.0, 2.0, 0.0
    if "ram_over" in name:
        ram = 32.0
    if "gpu_over" in name:
        gpu = 24.0
    got_local = _decide(row, local, ctx, cpu=cpu, ram=ram, gpu=gpu)
    got_remote = _decide(row, remote, ctx, cpu=cpu, ram=ram, gpu=gpu)
    assert got_local.ok == expect_ok, f"local {name}: {got_local}"
    assert got_remote.ok == got_local.ok, (
        f"parity {name}: local={got_local} remote={got_remote}"
    )


def test_t026_dispatch_local_only_differs():
    row = _task(dispatch="local")
    local, remote = _twin_nodes()
    ctx = _ctx()
    assert _decide(row, local, ctx).ok is True
    assert _decide(row, remote, ctx).ok is False


def test_exclusive_project_allows_other_projects():
    """exclusive_project blocks same project only — other projects still admit."""
    local, remote = _twin_nodes()
    remote.projects = ["proj", "other"]
    row = _task(project="other")
    ctx = _ctx(exclusive_projects={"proj": "proj/ex"}, n_running_by_proj={"proj": 1}, n_running=1)
    assert _decide(row, local, ctx).ok is True
    assert _decide(row, remote, ctx).ok is True
    same = _task(project="proj")
    assert _decide(same, local, ctx).ok is False


def test_node_from_system_and_worker_roundtrip():
    local = node_from_system(
        {
            "os": {"system": "Windows", "machine": "AMD64"},
            "ram_total_gib": 32.0,
            "cpu_total_cores": 16,
            "gpu_vram_total_gib": 0.0,
            "gpus": [],
        }
    )
    assert local.node_id == MASTER_NODE_ID
    assert local.is_local is True
    assert local.os == "windows"
    remote = node_from_worker(
        {
            "worker_id": "w1",
            "os": "windows",
            "arch": "x86_64",
            "ram_gib": 16.0,
            "cpu_cores": 8,
            "gpu_vram_gib": 0.0,
            "tags": "[]",
            "projects": '["proj"]',
            "gpus_json": "[]",
            "load_json": '{"ram_used_gib": 4.0}',
            "status": "online",
        }
    )
    assert remote.is_local is False
    assert remote.ram_free_gib == pytest.approx(12.0)
    # G6: cpu_free (8 cores) contributes 8 * 0.01 = 0.08 as tie-breaker
    assert score_assignment(_task(), remote, 4.0) == pytest.approx(8.0 + 8 * 0.01)
    # PC-1a: GPU term adds leftover VRAM when gpu > 0
    remote_gpu = Node(
        node_id="w-gpu",
        is_local=False,
        projects=["proj"],
        os="windows",
        arch="x86_64",
        ram_gib=16.0,
        cpu_cores=8.0,
        gpu_vram_gib=12.0,
        ram_used_gib=4.0,
        vram_used_gib=2.0,
        cpu_used=0.0,
    )
    assert score_assignment(_task(), remote_gpu, 4.0, 4.0) == pytest.approx(
        (16.0 - 4.0 - 4.0) + (12.0 - 2.0 - 4.0) + 8 * 0.01
    )
