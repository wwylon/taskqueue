"""Admission dry-run (taskqueue.plan / GET /api/plan)."""
from __future__ import annotations

from taskqueue.plan import build_plan
from tests.helpers.master_worker import http_request, make_handler_env

SYSTEM = {"cpu_total_cores": 8, "ram_total_gib": 64.0, "gpu_vram_total_gib": 0.0, "gpus": []}
CFG = {"cpu_limit_pct": 100, "ram_limit_pct": 100}


def _add(db, tid, *, prio=0, ram=1.0, status="queued", deps="[]"):
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, status, enabled, priority, "
        "resources, depends_on, enqueued_at, updated_at) "
        "VALUES ('p',?,'x','[]',?,1,?,?,?,'2026-01-01','2026-01-01')",
        (tid, status, prio, f'{{"cpu_cores": 1, "ram_gib": {ram}}}', deps),
    )
    db.commit()


def test_plan_orders_and_blocks_on_resources_and_deps(tmp_path, monkeypatch):
    _, db, _ = make_handler_env(tmp_path)
    monkeypatch.setattr("taskqueue.resources.ram_used_gib", lambda: 0.0)
    _add(db, "big", prio=5, ram=100.0)
    _add(db, "small", prio=1, ram=1.0)
    _add(db, "dep", prio=0, deps='["p/small"]')
    plan = build_plan(db, CFG, SYSTEM)
    by = {e["task_id"]: e for e in plan["tasks"]}
    assert by["big"]["verdict"] == "blocked" and "RAM" in by["big"]["reason"]
    assert by["small"]["verdict"] == "start" and by["small"]["start_order"] == 1
    assert by["dep"]["verdict"] == "blocked" and "waiting on" in by["dep"]["reason"]
    assert plan["would_start"] == 1 and plan["approximate"] is True


def test_plan_endpoint(tmp_path):
    handler, db, _ = make_handler_env(tmp_path)
    _add(db, "a")
    status, body = http_request(handler, "GET", "/api/plan?limit=5")
    assert status == 200 and body["tasks"][0]["task_id"] == "a"


def test_plan_marks_paused_project_blocked(tmp_path, monkeypatch):
    _, db, _ = make_handler_env(tmp_path)
    monkeypatch.setattr("taskqueue.resources.ram_used_gib", lambda: 0.0)
    monkeypatch.setattr("taskqueue.db.blocked_projects", lambda conn: ["p"])
    _add(db, "a")
    plan = build_plan(db, CFG, SYSTEM)
    assert plan["tasks"][0]["verdict"] == "blocked"
    assert plan["would_start"] == 0


def test_plan_gpu_daily_budget_blocks_gpu_task_only(tmp_path, monkeypatch):
    _, db, _ = make_handler_env(tmp_path)
    monkeypatch.setattr("taskqueue.resources.ram_used_gib", lambda: 0.0)
    monkeypatch.setattr("taskqueue.plan.get_all_project_limits", lambda c: None, raising=False)
    monkeypatch.setattr(
        "taskqueue.db.get_all_project_limits", lambda c: {"p": {"max_gpu_hours_day": 2.0}}
    )
    monkeypatch.setattr("taskqueue.db.gpu_hours_today", lambda c, p: 2.5)
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, status, enabled, priority, "
        "resources, depends_on, enqueued_at, updated_at) VALUES "
        "('p','g','x','[]','queued',1,5,'{\"cpu_cores\":1,\"ram_gib\":1,\"gpu_vram_gib\":2}','[]','2026-01-01','2026-01-01')"
    )
    db.commit()
    _add(db, "cpu")
    system = dict(SYSTEM, gpu_vram_total_gib=24.0)
    plan = build_plan(db, CFG, system)
    by = {e["task_id"]: e for e in plan["tasks"]}
    assert by["g"]["verdict"] == "blocked" and "max_gpu_hours_day" in by["g"]["reason"]
    assert by["cpu"]["verdict"] == "start"
    assert plan["gpu_budget"]["p"] == {"limit_hours": 2.0, "used_hours": 2.5, "remaining_hours": 0.0}
