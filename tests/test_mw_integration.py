"""In-process mini-cluster integration tests."""

from __future__ import annotations

import threading
from pathlib import Path

from tests.helpers.master_worker import (
    FakeWorkerClient,
    mini_cluster,
    seed_queued_task,
)


def test_happy_path_claim_log_finalize(tmp_path: Path, monkeypatch) -> None:
    with mini_cluster(tmp_path, monkeypatch=monkeypatch) as cluster:
        seed_queued_task(cluster.db, dispatch="remote", task_id="JOB1")
        client = FakeWorkerClient(cluster.base_url)
        assert client.register()["ok"] is True
        claim = client.claim(wait=0)
        assert claim.get("task") is not None
        run_id = int(claim["run_id"])
        token = claim["finalize_token"]
        assert client.log_chunk(run_id, "hello from worker\n(100.0%)\n")["ok"] is True
        fin = client.finalize(run_id, token, status="completed", exit_code=0)
        assert fin["ok"] is True
        row = cluster.db.execute("SELECT status FROM tasks WHERE task_id='JOB1'").fetchone()
        assert row["status"] == "completed"
        log_path = Path(claim["log_path"])
        assert log_path.is_file()
        assert "hello from worker" in log_path.read_text(encoding="utf-8")


def test_t031_eight_workers_one_task_via_http(tmp_path: Path, monkeypatch) -> None:
    with mini_cluster(tmp_path, monkeypatch=monkeypatch) as cluster:
        seed_queued_task(cluster.db, dispatch="remote", task_id="ONLY")
        results: list[dict] = []
        lock = threading.Lock()

        def worker_go(wid: str) -> None:
            c = FakeWorkerClient(cluster.base_url, worker_id=wid, session_id=f"s-{wid}")
            c.register()
            r = c.claim(wait=0) or {}
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker_go, args=(f"w{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        claimed = [r for r in results if r.get("task")]
        assert len(claimed) == 1
        runs = cluster.db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        assert runs == 1
