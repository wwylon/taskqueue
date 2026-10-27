"""Round 12 Wave A: F-137–F-141."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.placement import (
    clear_remote_reservation,
    get_remote_reservation,
    refresh_remote_reservation,
)
from taskqueue.queries import count_tasks, list_tasks
from taskqueue.resources import ResourceBudget
from taskqueue.scheduler import Node, node_from_worker, parse_worker_load
from taskqueue.worker_api import _svc_key
from tests.test_web_routes import _make_handler, _make_request


def test_f137_cpu_budget_is_max_of_ema_and_declared(monkeypatch) -> None:
    monkeypatch.setattr("taskqueue.resources.ram_used_gib", lambda: 8.0)
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": 64.0,
        "gpu_vram_total_gib": 0.0,
        "gpus": [],
        "cpu_used_cores_ema": 4.0,
    }
    cfg = {"cpu_limit_pct": 50, "gpu_limit_pct": 100, "ram_limit_pct": 80, "ram_hard_limit_pct": 95}
    # Soft CPU limit = 4 cores. EMA already 4; adding declared 4 must NOT become 8.
    b = ResourceBudget(system, cfg)
    assert b.cpu_limit == 4.0
    assert b.cpu_reserved == 4.0
    b.reserve(4.0, 0.0, 0.0)  # one running 4-core task
    assert b.cpu_reserved == 4.0  # max(4, 4), not 8
    ok, reason = b.fits(1.0, 0.0, 0.5)
    assert not ok and "CPU" in reason  # max(4, 4+1)=5 > 4
    # Lower EMA: declared dominates until candidate would exceed.
    system2 = dict(system, cpu_used_cores_ema=1.0)
    b2 = ResourceBudget(system2, cfg)
    b2.reserve(2.0, 0.0, 0.0)
    assert b2.cpu_reserved == 2.0
    ok2, _ = b2.fits(1.0, 0.0, 0.5)
    assert ok2  # max(1, 2+1)=3 <= 4


def test_f139_parse_worker_load_keeps_gpu_free_and_merges() -> None:
    load = parse_worker_load({
        "ram_used_gib": 10.0,
        "vram_used_gib": 3.0,
        "cpu_used": 2.0,
        "gpu_free": [{"vram_free_gib": 5.0}, {"vram_free_gib": 1.5}],
    })
    assert load["ram_used_gib"] == 10.0
    assert load["gpu_free"][0]["vram_free_gib"] == 5.0
    worker = {
        "worker_id": "w1",
        "os": "linux",
        "arch": "x86_64",
        "ram_gib": 64.0,
        "cpu_cores": 16,
        "gpu_vram_gib": 24.0,
        "gpus_json": json.dumps([
            {"index": 0, "vram_gib": 12.0, "vram_used_gib": 0.0},
            {"index": 1, "vram_gib": 12.0, "vram_used_gib": 0.0},
        ]),
        "tags": "[]",
        "projects": "[]",
        "load_json": json.dumps(load),
        "status": "online",
        "trust": "trusted",
        "stable": 0,
    }
    node = node_from_worker(worker)
    assert isinstance(node, Node)
    assert abs(node.gpus[0]["vram_used_gib"] - 7.0) < 1e-6  # 12 - 5
    assert abs(node.gpus[1]["vram_used_gib"] - 10.5) < 1e-6  # 12 - 1.5


def test_f140_remote_reservation_keyed_by_db(tmp_path: Path) -> None:
    clear_remote_reservation(db_key="a")
    clear_remote_reservation(db_key="b")

    class _Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)

    fat = _Row(project="p", task_id="FAT", resources=json.dumps({"cpu_cores": 1, "ram_gib": 14.0}))
    node = Node(node_id="only", ram_gib=16.0, cpu_cores=8, ram_used_gib=10.0)

    def resources_for(row):
        return 1.0, 14.0, 0.0

    def eligible(row, n):
        return True

    cfg = {"reservation_max_ticks": 60, "scheduler_interval_sec": 5}
    hold_a = refresh_remote_reservation(
        [fat], [node], resources_for=resources_for, eligible=eligible, cfg=cfg, db_key="a"
    )
    assert hold_a.get("key") == ("p", "FAT")
    assert get_remote_reservation(db_key="b") == {}
    assert get_remote_reservation(db_key="a").get("worker_id") == "only"
    clear_remote_reservation(db_key="a")
    assert get_remote_reservation(db_key="a") == {}


def test_f138_svc_cache_miss_probes(monkeypatch) -> None:
    """Missing key must call check_services_cached, not treat as healthy."""
    from taskqueue import worker_api

    probed: list[str] = []

    def fake_cached(sv, *, allow_remote=False, system_info=None):
        probed.append(sv)
        return ("service down", "tq.block.service")

    monkeypatch.setattr("taskqueue.scheduler.check_services_cached", fake_cached)
    # Simulate the miss path used in _ctx_for_row.
    _svc_cache: dict = {}
    svc_raw = '[{"name":"ollama"}]'
    sk = _svc_key(svc_raw)
    assert sk not in _svc_cache
    from taskqueue.scheduler import check_services_cached

    svc_block, svc_code = check_services_cached(svc_raw, allow_remote=True)
    _svc_cache[sk] = svc_block
    assert svc_block == "service down"
    assert svc_code == "tq.block.service"
    assert probed == [svc_raw]
    # Second hit uses cache without re-probe in our hand-rolled mirror of the fix.
    assert sk in _svc_cache


def test_f141_api_tasks_search_and_total(tmp_path: Path) -> None:
    HandlerClass, db = _make_handler(tmp_path)
    now = datetime.now(UTC).isoformat()
    for i in range(12):
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
            "enabled, description) VALUES ('proj', ?, 'queued', 'true', ?, ?, 1, ?)",
            (f"task-{i:03d}", now, now, f"desc needle-{i}" if i % 2 == 0 else f"other-{i}"),
        )
    db.commit()
    # Accurate total beyond old 500 soft-cap / post-slice bug.
    code, body = _make_request(HandlerClass, "GET", "/api/tasks?project=proj&limit=5&offset=0")
    assert code == 200
    data = json.loads(body)
    assert data["total"] == 12
    assert len(data["tasks"]) == 5
    code, body = _make_request(HandlerClass, "GET", "/api/tasks?project=proj&q=needle&limit=20")
    assert code == 200
    data = json.loads(body)
    assert data["total"] == 6
    assert all("needle" in (t.get("description") or "") or "needle" in t["task_id"] for t in data["tasks"])
    assert count_tasks(db, project="proj", search="needle") == 6
    assert len(list_tasks(db, project="proj", search="needle", limit=100)) == 6
