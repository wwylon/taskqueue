import sqlite3
from datetime import UTC, datetime

from taskqueue import config as cfg
from taskqueue.config import Project
from tests.helpers.master_worker import http_request, make_handler_env


def test_snapshot_served_and_removed(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setattr(cfg, "state_home", lambda: home)
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    src = sqlite3.connect(root / "data" / "d.db")
    src.execute("create table t(x)")
    src.execute("insert into t values (1)")
    src.commit()
    src.close()
    proj = Project(name="proj", root=root, queue_dir=root / "q")
    H, db, _ = make_handler_env(tmp_path / "h", {"proj": proj}) if (tmp_path / "h").mkdir() is None else (None, None, None)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at) VALUES ('proj','T','x','running',?,?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finalize_token) VALUES ('proj','T','running',?, 'tok')",
        (now,),
    )
    db.commit()
    rid = db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    # Paths outside data/ (or data.inputs) are rejected (Round 12 snapshot allowlist).
    bad, _ = http_request(H, "GET", f"/api/data/snapshot/{rid}/secret.db", extra_headers={"X-TQ-Finalize-Token": "tok"})
    assert bad == 403
    status, body = http_request(H, "GET", f"/api/data/snapshot/{rid}/data/d.db", extra_headers={"X-TQ-Finalize-Token": "tok"})
    assert status == 200
    assert not list((home / "snapshots").glob("*"))
