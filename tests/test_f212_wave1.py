"""Wave 1 correctness: F-212 cuda/driver split, F-213 CLI leftovers, F-214 admit helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.master_worker import seed_queued_task


def test_get_sys_info_keeps_driver_and_cuda_distinct(monkeypatch) -> None:
    from taskqueue import worker

    class _R:
        returncode = 0
        stdout = "GPU0, 8192, 4096, 8.9, 560.94, 12.6\n"
        stderr = ""

    import taskqueue.resources as resources

    monkeypatch.setattr(resources, "_detect_gpus", lambda: [])
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(worker.psutil, "virtual_memory", lambda: SimpleNamespace(total=8 * 1024**3))
    monkeypatch.setattr(worker.psutil, "cpu_count", lambda logical=True: 4)

    info = worker.get_sys_info()
    assert info["driver_version"] == "560.94"
    assert info["cuda_version"] == "12.6"
    assert info["gpus"][0]["driver_version"] == "560.94"
    assert info["gpus"][0]["cuda_version"] == "12.6"
    assert info["driver_version"] != info["cuda_version"]


def test_reject_leftover_stop_and_status(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskqueue import cli

    monkeypatch.setattr(cli, "cmd_stop", lambda *a, **k: pytest.fail("must not act"))
    monkeypatch.setattr(cli, "cmd_status", lambda *a, **k: pytest.fail("must not act"))
    assert cli._cmd_stop(tmp_path, ["p/t", "extra"]) == 2
    assert "Unexpected" in capsys.readouterr().out
    assert cli._cmd_status(tmp_path, ["--json", "nope"]) == 2
    assert "Unexpected" in capsys.readouterr().out


def test_load_fairshare_candidates_orders_by_age_bonus(tmp_path: Path) -> None:
    from taskqueue.admit_context import load_fairshare_candidates
    from taskqueue.db import open_db

    conn = open_db(tmp_path / "queue.db")
    seed_queued_task(conn, project="p", task_id="low_old", dispatch="local")
    seed_queued_task(conn, project="p", task_id="high_fresh", dispatch="local")
    now = datetime.now(UTC)
    old = (now - timedelta(minutes=40)).isoformat()
    fresh = now.isoformat()
    conn.execute(
        "UPDATE tasks SET priority=10, enqueued_at=? WHERE project='p' AND task_id='low_old'",
        (old,),
    )
    conn.execute(
        "UPDATE tasks SET priority=11, enqueued_at=? WHERE project='p' AND task_id='high_fresh'",
        (fresh,),
    )
    conn.commit()
    queued, occ, usage, status_map, running = load_fairshare_candidates(conn, limit=64)
    # 40 min wait → +8 age bonus → effective 18 beats priority 11.
    assert [r["task_id"] for r in queued] == ["low_old", "high_fresh"]
    assert "gpu_running_by_proj" in occ
    assert usage == {}
    assert ("p", "low_old") in status_map
    assert running == []
    conn.close()
