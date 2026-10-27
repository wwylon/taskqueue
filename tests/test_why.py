"""tq why / /api/tasks/<p>/<t>/why / MCP why_not_running."""
from __future__ import annotations

from pathlib import Path

from taskqueue.why import explain_task, format_explanation
from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task

SYSTEM = {"cpu_total_cores": 8, "ram_total_gib": 64.0, "gpu_vram_total_gib": 0.0, "gpus": []}


def test_queued_task_with_unmet_dependency_and_block_reason(tmp_path: Path) -> None:
    _h, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="up", dispatch="local")
    seed_queued_task(db, project="p", task_id="down", dispatch="local")
    db.execute("UPDATE tasks SET depends_on='[\"up\"]', block_reason='dependency p/up not done' WHERE task_id='down'")
    db.commit()
    info = explain_task(db, "p", "down", system=SYSTEM)
    assert info["ok"] and info["status"] == "queued"
    assert any("dependency" in r for r in info["reasons"])
    assert info["plan"]["verdict"] == "blocked"
    assert "p/down: queued" in format_explanation(info)


def test_free_task_should_start_and_unknown_task_404(tmp_path: Path, monkeypatch) -> None:
    # Admission reads live host RAM; pin it so the test does not depend on the machine.
    monkeypatch.setattr("taskqueue.resources.ram_used_gib", lambda: 1.0)
    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="free", dispatch="local")
    info = explain_task(db, "p", "free", system=SYSTEM)
    assert info["plan"]["verdict"] == "start" and "should start" in info["reasons"][0]
    status, body = http_request(H, "GET", "/api/tasks/p/nope/why")
    assert status == 404 and body["ok"] is False


def test_disabled_and_running(tmp_path: Path) -> None:
    _h, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="off", dispatch="local", enabled=0)
    seed_queued_task(db, project="p", task_id="run", dispatch="local")
    db.execute("UPDATE tasks SET status='running' WHERE task_id='run'")
    db.commit()
    assert any("disabled" in r for r in explain_task(db, "p", "off", system=SYSTEM)["reasons"])
    assert explain_task(db, "p", "run", system=SYSTEM)["reasons"] == ["it is running"]
