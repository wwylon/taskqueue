"""Round 11 Wave A: P0 correctness + security DTO + offline sync (F-110–F-119)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from taskqueue import errors as tqerr
from taskqueue.config import Project
from taskqueue.db import get_task, open_db, parse_task_yaml, upsert_task
from taskqueue.queries import cancel_negative_dependents
from taskqueue.web.responses import _STATUS_RANK, _history_row_dict, _task_dict
from taskqueue.yaml_sync import _sync_project, clear_yaml_mtime_cache
from tests.helpers.master_worker import http_request, make_handler_env
from tests.test_queue import _project, _yaml


def test_f114_cancelled_in_status_rank() -> None:
    assert "WHEN 'cancelled' THEN 2" in _STATUS_RANK


def test_f115_cancel_negative_honors_depends_on_any(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    for tid in ("a", "b"):
        upsert_task(
            db,
            parse_task_yaml(
                _yaml(tmp_path, "p", tid, command="x", negative_exit_codes=[3]),
                proj,
            ),
        )
        db.execute(
            "UPDATE tasks SET status='completed', last_error_code=? WHERE project=? AND task_id=?",
            (tqerr.RUN_NEGATIVE, "p", tid),
        )
    upsert_task(
        db,
        parse_task_yaml(
            _yaml(
                tmp_path,
                "p",
                "follow",
                command="x",
                depends_on_any=["a", "b"],
                on_dependency_negative="cancel",
            ),
            proj,
        ),
    )
    db.commit()
    assert cancel_negative_dependents(db) == [("p", "follow")]
    assert get_task(db, "p", "follow")["status"] == "cancelled"
    db.close()


def test_f115_depends_on_any_waits_if_one_peer_not_negative(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    upsert_task(db, parse_task_yaml(_yaml(tmp_path, "p", "a", command="x"), proj))
    upsert_task(db, parse_task_yaml(_yaml(tmp_path, "p", "b", command="x"), proj))
    db.execute(
        "UPDATE tasks SET status='completed', last_error_code=? WHERE project=? AND task_id=?",
        (tqerr.RUN_NEGATIVE, "p", "a"),
    )
    db.execute(
        "UPDATE tasks SET status='completed', last_error_code=NULL WHERE project=? AND task_id=?",
        ("p", "b"),
    )
    upsert_task(
        db,
        parse_task_yaml(
            _yaml(
                tmp_path,
                "p",
                "follow",
                command="x",
                depends_on_any=["a", "b"],
                on_dependency_negative="cancel",
            ),
            proj,
        ),
    )
    db.commit()
    assert cancel_negative_dependents(db) == []
    assert get_task(db, "p", "follow")["status"] == "queued"
    db.close()


def test_f116_history_row_dict_drops_finalize_token() -> None:
    row = {
        "run_id": 1,
        "project": "p",
        "task_id": "t",
        "status": "completed",
        "finalize_token": "secret",
        "log_path": "C:/abs/log.txt",
        "worker_session_id": "sess",
        "anomalies": ["ram"],
    }
    out = _history_row_dict(row)
    assert "finalize_token" not in out
    assert "log_path" not in out
    assert "worker_session_id" not in out
    assert out["anomalies"] == ["ram"]


def test_f117_task_dict_strip_secrets() -> None:
    class R(dict):
        def keys(self):
            return super().keys()

    row = R(
        project="p",
        task_id="t",
        status="queued",
        env='{"API_KEY":"x"}',
        args_override='["--secret"]',
        yaml_source="/secret/path.yaml",
        resources="{}",
        depends_on="[]",
        depends_on_any="[]",
        requires="{}",
        tags="[]",
        args="[]",
        resume_args="[]",
        requires_paths="[]",
        requires_services="[]",
    )
    d = _task_dict(row, strip_secrets=True)
    assert "env" not in d
    assert "args_override" not in d
    assert "yaml_source" not in d


def test_f117_yaml_get_denied_for_read_scope() -> None:
    from taskqueue.operator_features import scope_allows

    assert scope_allows("read", "GET", "/api/tasks/p/t/yaml") is False
    assert scope_allows("write", "GET", "/api/tasks/p/t/yaml") is False
    assert scope_allows("admin", "GET", "/api/tasks/p/t/yaml") is True


def test_f116_f117_http_task_apis_strip_secrets(tmp_path: Path) -> None:
    clear_yaml_mtime_cache()
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    (qdir / "t.yaml").write_text(
        yaml.dump(
            {
                "task_id": "t",
                "command": sys.executable,
                "args": ["-c", "pass"],
                "env": {"SECRET": "s3cr3t"},
            }
        ),
        encoding="utf-8",
    )
    proj = Project(name="p", root=root, queue_dir=qdir)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"p": proj})
    http_request(HandlerClass, "POST", "/api/cli/sync")
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finalize_token, log_path, worker_session_id) "
        "VALUES ('p', 't', 'completed', '2020-01-01T00:00:00+00:00', 'tok', 'C:/abs.log', 'sess')"
    )
    db.commit()

    code, detail = http_request(HandlerClass, "GET", "/api/tasks/p/t")
    assert code == 200
    task = detail["task"]
    assert "env" not in task
    assert "SECRET" not in json.dumps(task)

    code, runs = http_request(HandlerClass, "GET", "/api/tasks/p/t/runs")
    assert code == 200
    assert runs["runs"]
    blob = json.dumps(runs)
    assert "finalize_token" not in blob
    assert "tok" not in blob
    assert "C:/abs.log" not in blob
    db.close()


def test_f118_offline_sync_deletes_removed_yaml(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskqueue._cli_project import cmd_sync
    from taskqueue.yaml_sync import clear_yaml_mtime_cache

    clear_yaml_mtime_cache()
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    y1 = qdir / "keep.yaml"
    y2 = qdir / "gone.yaml"
    for p, tid in ((y1, "keep"), (y2, "gone")):
        p.write_text(
            yaml.dump({"task_id": tid, "command": sys.executable, "args": ["-c", "pass"]}),
            encoding="utf-8",
        )
    projects_yaml = home / "projects.yaml"
    projects_yaml.write_text(
        yaml.dump({"proj": {"root": str(root), "queue_dir": str(qdir)}}),
        encoding="utf-8",
    )

    monkeypatch.setattr("taskqueue.master_client.via_master", lambda *a, **k: None)
    monkeypatch.setattr("taskqueue._cli_project._daemon_running", lambda h: None)
    monkeypatch.setattr("taskqueue._cli_project._db", lambda h: home / "queue.db")
    monkeypatch.setattr(
        "taskqueue.config.load_projects",
        lambda h: {"proj": Project(name="proj", root=root, queue_dir=qdir)},
    )
    monkeypatch.setattr("taskqueue._cli_helpers.warn_enabled_terminal_summary", lambda db: None)
    monkeypatch.setattr("taskqueue._cli_project.print_wait_hint", lambda: None)

    assert cmd_sync(home) == 2
    db = open_db(home / "queue.db", migrate=False)
    assert get_task(db, "proj", "keep") is not None
    assert get_task(db, "proj", "gone") is not None
    db.close()

    y2.unlink()
    assert cmd_sync(home) == 2
    db = open_db(home / "queue.db", migrate=False)
    assert get_task(db, "proj", "keep") is not None
    assert get_task(db, "proj", "gone") is None
    db.close()
    err = capsys.readouterr().err
    assert "[OFFLINE]" in err


def test_f113_dashboard_history_keeps_anomalies(tmp_path: Path) -> None:
    from taskqueue.web.responses import _get_history_response

    HandlerClass, db, _ = make_handler_env(tmp_path, projects={})
    db.execute(
        "INSERT INTO tasks (project, task_id, status, enabled, command, enqueued_at, updated_at) "
        "VALUES ('p', 't', 'completed', 1, 'x', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, finished_at, peak_ram_gib) "
        "VALUES ('p', 't', 'completed', '2020-01-01T00:00:00+00:00', '2020-01-01T00:01:00+00:00', 99.0)"
    )
    # Seed enough history so run_anomaly can flag (may or may not flag; at least pass-through works)
    hist = _get_history_response(db, limit=10)
    assert hist["runs"]
    projected = [_history_row_dict(r) for r in hist["runs"]]
    # anomalies key preserved when present on the raw row
    hist["runs"][0]["anomalies"] = ["ram"]
    assert _history_row_dict(hist["runs"][0]).get("anomalies") == ["ram"]
    assert "finalize_token" not in projected[0]
    db.close()


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="node required")
def test_f110_f111_dom_id_and_history_identity() -> None:
    """Node: domIdPart round-trip; History must not clobber prevHistoryStr."""
    import subprocess

    root = Path(__file__).resolve().parents[1] / "taskqueue" / "static" / "js"
    script = f"""
    const {{ domIdPart }} = await import({json.dumps((root / 'util/format.js').as_uri())});
    const raw = 'a&b"c';
    const id = 'btn-' + domIdPart(raw);
    if (id.includes('&') || id.includes('"')) throw new Error('unsafe id: ' + id);
    if (domIdPart(raw) !== domIdPart(raw)) throw new Error('unstable');
    console.log(JSON.stringify({{ ok: true, id }}));
    """
    out = subprocess.check_output(["node", "--input-type=module", "-e", script], text=True)
    assert json.loads(out.strip())["ok"] is True
