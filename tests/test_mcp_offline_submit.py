from pathlib import Path

from taskqueue import mcp_server
from taskqueue.config import Project
from taskqueue.db import open_db
from taskqueue.config import db_path


def test_offline_submit_persists_into_queue_dir(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    open_db(db_path(home)).close()
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    root.mkdir()
    proj = Project(name="p", root=root, queue_dir=qdir)
    monkeypatch.setattr("taskqueue.config.load_projects", lambda h: {"p": proj})
    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    yaml_text = "task_id: t1\ncommand: echo hi\n"
    r1 = mcp_server._submit_task_nowait(home, {"project": "p", "yaml_text": yaml_text})
    r2 = mcp_server._submit_task_nowait(
        home, {"project": "p", "yaml_text": "task_id: t2\ncommand: echo hi\n"}
    )
    assert r1["ok"], r1
    assert r2["ok"], r2
    files = sorted(qdir.glob("submitted_*.yaml"))
    assert len(files) == 2
    assert not list(qdir.glob(".*.tmp"))
    bad = mcp_server._submit_task_nowait(home, {"project": "p", "yaml_text": "task_id: [\n"})
    assert bad["ok"] is False
    assert len(list(qdir.glob("*.yaml"))) == 2
