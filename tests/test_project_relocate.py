"""F-240: set_project_root, yaml_source rewrite, release/relocate HTTP + CLI."""

from __future__ import annotations

from pathlib import Path

from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task
from tqdaemon import _cli_project
from tqdaemon.config import DEFAULT_QUEUE_DIR, Project
from tqdaemon.db import open_db
from tqdaemon.project_relocate import (
    relocate_project_files,
    rewrite_yaml_source_prefix,
    set_project_root,
    status_histogram,
)


def _write_registry(home: Path, name: str, root: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.yaml").write_text(
        f"projects:\n  {name}:\n    root: {root.as_posix()}\n",
        encoding="utf-8",
    )


def _make_tree(root: Path) -> Path:
    q = root / DEFAULT_QUEUE_DIR
    q.mkdir(parents=True)
    return q


def test_set_project_root_and_yaml_source_rewrite(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = tmp_path / "oldproj"
    new = tmp_path / "games" / "oldproj"
    _make_tree(old)
    _make_tree(new)
    _write_registry(home, "shed", old)
    db = open_db(home / "queue.db")
    seed_queued_task(db, project="shed", task_id="t1", dispatch="local")
    old_yaml = str(old / DEFAULT_QUEUE_DIR / "t1.yaml")
    db.execute("UPDATE tasks SET yaml_source = ? WHERE project='shed'", (old_yaml,))
    db.commit()

    prev = set_project_root(home, "shed", new)
    assert prev == old or prev.as_posix() == old.as_posix()
    text = (home / "projects.yaml").read_text(encoding="utf-8")
    assert str(new) in text or new.as_posix() in text

    n = rewrite_yaml_source_prefix(db, old, new)
    assert n == 1
    row = db.execute("SELECT yaml_source FROM tasks WHERE task_id='t1'").fetchone()
    assert str(new) in row["yaml_source"] or new.as_posix().replace("/", "\\") in row["yaml_source"].replace("/", "\\")
    db.close()


def test_relocate_project_files_refuses_missing_queue_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = tmp_path / "old"
    new = tmp_path / "new"
    _make_tree(old)
    new.mkdir(parents=True)  # no queue dir
    _write_registry(home, "p", old)
    db = open_db(home / "queue.db")
    try:
        relocate_project_files(home, db, "p", new)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "queue dir" in str(exc)
    db.close()


def test_relocate_offline_preserves_status_histogram(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = tmp_path / "old"
    new = tmp_path / "portfolio" / "old"
    _make_tree(old)
    _make_tree(new)
    _write_registry(home, "p", old)
    db = open_db(home / "queue.db")
    seed_queued_task(db, project="p", task_id="a", dispatch="local")
    seed_queued_task(db, project="p", task_id="b", dispatch="local")
    db.execute("UPDATE tasks SET status='completed' WHERE task_id='b'")
    db.execute(
        "UPDATE tasks SET yaml_source = ? WHERE task_id='a'",
        (str(old / DEFAULT_QUEUE_DIR / "a.yaml"),),
    )
    db.execute(
        "UPDATE tasks SET yaml_source = ? WHERE task_id='b'",
        (str(old / DEFAULT_QUEUE_DIR / "b.yaml"),),
    )
    db.commit()
    before = status_histogram(db, "p")
    summary = relocate_project_files(home, db, "p", new)
    assert summary["ok"] is True
    assert summary["before"]["status"] == before
    assert summary["yaml_source_rewritten"] == 2
    assert Path(summary["backup"]).is_file()
    assert status_histogram(db, "p") == before
    db.close()


def test_release_relocate_http_with_callbacks(tmp_path: Path) -> None:
    home = tmp_path
    old = tmp_path / "proj"
    new = tmp_path / "moved"
    q_old = _make_tree(old)
    _make_tree(new)
    _write_registry(home, "p", old)

    class Obs:
        def __init__(self):
            self.live = {}

        def schedule(self, handler, path, recursive=False):
            self.live[path] = object()
            return path

        def unschedule(self, watch):
            del self.live[watch]

    from tqdaemon import daemon

    obs = Obs()
    watches = daemon._QueueDirWatches(obs, handler=None)
    projects = {"p": Project(name="p", root=old, queue_dir=q_old)}
    watches.update(projects)

    released = []

    def release_cb(name: str) -> dict:
        released.append(name)
        watches.release(name, projects)
        return {"ok": True, "project": name, "released": True}

    def relocate_cb(name: str, new_root: str) -> dict:
        from tqdaemon.project_relocate import relocate_project_files

        watches.release(name, projects)
        db = open_db(home / "queue.db")
        try:
            summary = relocate_project_files(home, db, name, Path(new_root))
        finally:
            db.close()
        summary.pop("project_obj", None)
        watches.clear_release(name)
        return summary

    db = open_db(home / "queue.db")
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    db.execute(
        "UPDATE tasks SET yaml_source = ?",
        (str(old / DEFAULT_QUEUE_DIR / "t.yaml"),),
    )
    db.commit()
    db.close()

    from tqdaemon.web.handler import make_handler

    H = make_handler(
        open_db(home / "queue.db"),
        {},
        {},
        lambda: projects,
        home / "queue.db",
        release_project=release_cb,
        relocate_project=relocate_cb,
    )
    status, body = http_request(H, "POST", "/api/project/release/p")
    assert status == 200 and body["ok"] is True and released == ["p"]
    assert str(q_old) not in obs.live

    status, body = http_request(H, "POST", "/api/project/relocate/p", {"new_root": str(new)})
    assert status == 200 and body["ok"] is True
    assert body["yaml_source_rewritten"] == 1


def test_release_refuses_running(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    db.execute("UPDATE tasks SET status='running'")
    db.commit()
    # No callback — still refuses on running before checking callback.
    status, body = http_request(H, "POST", "/api/project/release/p")
    assert status == 409 and body["ok"] is False


def test_cli_release_relocate_via_master(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_registry(tmp_path, "p", tmp_path / "p")
    (tmp_path / "p" / DEFAULT_QUEUE_DIR).mkdir(parents=True)
    open_db(tmp_path / "queue.db").close()
    calls = []

    def fake_via(home, method, path, **k):
        calls.append((method, path, k.get("body")))
        if "release" in path:
            return {"ok": True}
        return {
            "ok": True,
            "old_root": "/old",
            "new_root": "/new",
            "before": {"count": 1},
            "after": {"count": 1},
            "yaml_source_rewritten": 1,
            "backup": "/bak",
        }

    monkeypatch.setattr("tqdaemon.master_client.via_master", fake_via)
    assert _cli_project.cmd_release(tmp_path, "p") == 0
    assert any("release" in c[1] for c in calls)
    assert _cli_project.cmd_relocate(tmp_path, "p", str(tmp_path / "new")) == 0
    assert any("relocate" in c[1] for c in calls)
    out = capsys.readouterr().out
    assert "released" in out.lower() or "Released" in out or "released" in out
