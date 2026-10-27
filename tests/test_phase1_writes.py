"""Phase 1 write-path hardening: /api/cli/add file handling."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from taskqueue.config import Project
from tests.helpers.master_worker import http_request, make_handler_env
from tests.helpers.wait import wait_until


def _yaml(task_id: str) -> str:
    return yaml.dump({"task_id": task_id, "command": sys.executable, "args": ["-c", "pass"]})


def _env(tmp_path: Path):
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    proj = Project(name="p", root=root, queue_dir=qdir)
    HandlerClass, db, _ = make_handler_env(tmp_path, projects={"p": proj})
    return HandlerClass, db, qdir


def test_cli_add_without_filename_does_not_overwrite(tmp_path: Path) -> None:
    H, db, qdir = _env(tmp_path)
    for tid in ("one", "two"):
        code, data = http_request(H, "POST", "/api/cli/add", {"project": "p", "yaml_text": _yaml(tid)})
        assert code == 200 and data["ok"]
    assert len(list(qdir.glob("*.yaml"))) == 2
    db.close()


def test_cli_add_invalid_yaml_leaves_no_file(tmp_path: Path) -> None:
    H, db, qdir = _env(tmp_path)
    code, data = http_request(
        H, "POST", "/api/cli/add", {"project": "p", "filename": "bad.yaml", "yaml_text": "just: a mapping\n"}
    )
    assert code == 400 and not data["ok"]
    assert list(qdir.iterdir()) == []
    db.close()


def _seed_failed(db, args: str, overridable: str) -> None:
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, overridable_args, status, enabled, "
        "enqueued_at, updated_at) VALUES ('p','t','x',?,?, 'failed', 1, 'n', 'n')",
        (args, overridable),
    )
    db.commit()


def test_web_retry_rejects_non_overridable_arg(tmp_path: Path) -> None:
    H, db, _ = _env(tmp_path)
    _seed_failed(db, '["--a","1"]', '["--epochs"]')
    code, data = http_request(H, "POST", "/api/tasks/p/t/retry", {"args_override": ["--evil=1"]})
    assert code == 409 and not data["ok"]
    row = db.execute("SELECT status, args_override FROM tasks WHERE task_id='t'").fetchone()
    assert row["status"] == "failed" and row["args_override"] is None
    db.close()


def test_web_retry_accepts_overridable_arg(tmp_path: Path) -> None:
    H, db, _ = _env(tmp_path)
    _seed_failed(db, '["--a","1"]', '["--epochs"]')
    code, data = http_request(H, "POST", "/api/tasks/p/t/retry", {"args_override": ["--epochs=5"]})
    assert code == 200 and data["ok"]
    db.close()


def test_write_scope_cannot_edit_task_yaml() -> None:
    from taskqueue.operator_features import scope_allows

    assert scope_allows("write", "POST", "/api/tasks/p/t/retry")
    assert not scope_allows("write", "PUT", "/api/tasks/p/t/yaml")
    assert scope_allows("admin", "PUT", "/api/tasks/p/t/yaml")


def test_atomic_write_text_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    from taskqueue.fsutil import atomic_write_text

    f = tmp_path / "a.yaml"
    f.write_text("old", encoding="utf-8")
    atomic_write_text(f, "new: 1\n")
    assert f.read_text(encoding="utf-8") == "new: 1\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.yaml"]


def test_patch_enabled_targets_only_matching_document(tmp_path: Path) -> None:
    import pytest

    from taskqueue.yaml_patch import patch_task_yaml

    f = tmp_path / "multi.yaml"
    f.write_text(
        "task_id: a\ncommand: x\nenabled: true\n---\ntask_id: b\ncommand: y\nenabled: true\n---\n"
        "task_id: c\ncommand: z\n",
        encoding="utf-8",
    )
    patch_task_yaml(f, {"enabled": False}, task_id="b")
    docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
    assert [d.get("enabled") for d in docs] == [True, False, None]
    patch_task_yaml(f, {"enabled": False}, task_id="c")
    docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
    assert [d.get("enabled") for d in docs] == [True, False, False]
    with pytest.raises(ValueError):
        patch_task_yaml(f, {"enabled": False}, task_id="missing")
    assert [p.name for p in tmp_path.iterdir()] == ["multi.yaml"]


def test_runner_metrics_flush_uses_own_connection_in_reader_thread(tmp_path):
    import threading
    from unittest.mock import patch
    from taskqueue import runner

    seen = []

    class Fake:
        run_id = 7
        last_metrics = {"a": 1}
        log_counts = {}
        conn = object()
        db_path = tmp_path / "x.db"
        _owner_thread = -1  # not the calling thread

    def fake_update(db, run_id, payload):
        seen.append(db)

    import sqlite3
    sqlite3.connect(str(Fake.db_path)).close()
    with patch.object(runner, "update_run_metrics", fake_update):
        runner.ActiveRun._flush_metrics_json(Fake())
    assert len(seen) == 1 and seen[0] is not Fake.conn

    def boom(*a):
        raise RuntimeError("x")

    with patch.object(runner, "update_run_metrics", boom):
        runner.ActiveRun._flush_metrics_json(Fake())  # logged, not raised


def test_daemon_shutdown_handles_sigint_and_stops_in_parallel():
    src = (Path(__file__).resolve().parent.parent / "taskqueue" / "daemon.py").read_text(encoding="utf-8")
    assert "signal.signal(signal.SIGINT, _handle_sigterm)" in src
    assert "ThreadPoolExecutor" in src and "_safe_stop" in src
    assert "finally:\n        pid_path.unlink" in src.replace("\r\n", "\n")


def test_terminate_tree_kills_grandchildren():
    import subprocess
    import time
    import psutil

    from taskqueue.reap import terminate_tree

    code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(c.pid,flush=True);time.sleep(60)"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    child = int(p.stdout.readline())
    terminate_tree(p, graceful_sec=3)
    p.wait(timeout=10)
    wait_until(
        lambda: not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE,
        msg="grandchild still alive after terminate_tree",
    )


def test_tail_reader_joins_partial_lines(tmp_path):
    import threading
    import time
    from taskqueue import runner

    log = tmp_path / "t.log"
    log.write_text("", encoding="utf-8")
    got = []

    class P:
        rc = None

        def poll(self):
            return self.rc

    class Fake:
        proc = P()
        task_row = {"wall_clock_cap_sec": None}
        _log_path = log
        started_at = time.monotonic()
        _log_file = None

        def _handle_output_line(self, line, stamped, cap):
            got.append(line)
            return False

        def _finish_stream(self, crashed):
            pass

    f = Fake()
    t = threading.Thread(target=runner.ActiveRun._stream_output_tail, args=(f,))
    t.start()
    with open(log, "a", encoding="utf-8") as w:
        w.write("50%|abc")
        w.flush()
        time.sleep(0.4)  # deliberate: lets the tailer see the partial line first
        w.write("def 60%\nlast")
        w.flush()
    wait_until(lambda: len(got) >= 1, msg="tailer never emitted the joined line")
    f.proc.rc = 0
    t.join(5)
    assert got == ["50%|abcdef 60%\n", "last"]


def test_validate_task_spec_rejects_malformed(tmp_path):
    import pytest
    from taskqueue.db import validate_task_spec

    p = tmp_path / "t.yaml"
    for bad in (None, {}, [], {"task_id": "a"}, {"task_id": "a", "command": " "},
                {"task_id": "a\b", "command": "x"}, {"task_id": "a..b", "command": "x"},
                {"task_id": "a", "command": "x", "wall_clock_cap_sec": -1},
                {"task_id": "a", "command": "x", "resources": {"ram_gib": -2}}):
        with pytest.raises((ValueError, KeyError)):
            validate_task_spec(bad, p)
    validate_task_spec({"task_id": "a", "command": "x"}, p)


def test_concurrent_open_db_migrations_do_not_race(tmp_path):
    import threading
    from taskqueue.db import open_db

    db = tmp_path / "q.db"
    errs = []

    def go():
        try:
            open_db(db).close()
        except Exception as exc:  # pragma: no cover
            errs.append(exc)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs


def test_split2_raises_bad_request_without_sending(tmp_path):
    import pytest
    from taskqueue.web.handler import BadRequest, Handler

    class H:
        pass

    with pytest.raises(BadRequest):
        Handler._split2(H(), "noslash")
    assert Handler._split2(H(), "p/a%20b") == ("p", "a b")


def test_next_cron_fire_cache_matches_scan():
    from datetime import datetime, timedelta, UTC
    from taskqueue import operator_features as of

    of._CRON_FIRE_CACHE.clear()
    base = datetime(2026, 1, 1, 10, 7, 30, tzinfo=UTC)
    first = of.next_cron_fire("*/15 * * * *", base)
    assert first == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    for m in range(0, 8):
        assert of.next_cron_fire("*/15 * * * *", base + timedelta(minutes=m)) == first
    of._CRON_FIRE_CACHE.clear()
    assert of.next_cron_fire("*/15 * * * *", first) == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)


def test_validate_endpoint_dry_run(tmp_path: Path) -> None:
    H, db, qdir = _env(tmp_path)
    code, data = http_request(H, "POST", "/api/validate", {"project": "p", "yaml_text": _yaml("ok1")})
    assert code == 200 and data["valid"] and data["task_ids"] == ["ok1"]
    code, data = http_request(H, "POST", "/api/validate", {"project": "p", "yaml_text": "just: a mapping\n"})
    assert code == 200 and data["valid"] is False and data["error"]
    code, data = http_request(H, "POST", "/api/validate", {"project": "nope", "yaml_text": _yaml("x")})
    assert code == 404
    assert list(qdir.iterdir()) == []
    db.close()


def test_deps_dot_export(tmp_path: Path) -> None:
    from taskqueue.web.responses import deps_to_dot

    g = {
        "nodes": [{"id": "p/a-1", "status": "completed"}, {"id": 'p/"b"', "status": "queued", "stub": True}],
        "edges": [{"from": "p/a-1", "to": 'p/"b"', "health": "dead"}, {"from": "p/a-1", "to": "p/a-1", "health": "wait", "kind": "any"}],
    }
    dot = deps_to_dot(g)
    assert dot.startswith("digraph tq_deps {") and dot.rstrip().endswith("}")
    assert '"p/a-1" [label="p/a-1\\ncompleted"];' in dot
    assert '"p/\\"b\\""' in dot
    assert "color=red" in dot and 'style=dashed, label="any"' in dot


def test_deps_route_format(tmp_path: Path) -> None:
    H, db, _ = _env(tmp_path)
    code, data = http_request(H, "GET", "/api/deps?format=xml")
    assert code == 400
    code, data = http_request(H, "GET", "/api/deps?project=p")
    assert code == 200 and "nodes" in data
    db.close()


def test_audit_filters_and_export(tmp_path: Path) -> None:
    from taskqueue.operator_features import write_audit

    H, db, _ = _env(tmp_path)
    write_audit(db, "alice", "stop", "p", "t1", 'say "hi", ok')
    write_audit(db, "bob", "retry", "p", "t2", None)
    write_audit(db, "alice", "stop", "q", "t3", None)
    code, data = http_request(H, "GET", "/api/audit?actor=alice")
    assert code == 200 and [e["target"] for e in data["events"]] == ["t3", "t1"]
    code, data = http_request(H, "GET", "/api/audit?project=p&action=retry")
    assert [e["target"] for e in data["events"]] == ["t2"]
    first = http_request(H, "GET", "/api/audit?limit=1")[1]["events"][0]
    code, data = http_request(H, "GET", f"/api/audit?before_id={first['id']}&limit=1")
    assert data["events"][0]["target"] == "t2"
    code, data = http_request(H, "GET", "/api/audit?since=2999-01-01T00:00:00")
    assert data["events"] == []
    code, raw = http_request(H, "GET", "/api/audit?format=csv&actor=alice")
    text = raw.decode() if isinstance(raw, bytes) else raw
    assert text.splitlines()[0] == "id,ts,actor,action,project,target,detail"
    assert '"say ""hi"", ok"' in text
    code, raw = http_request(H, "GET", "/api/audit?format=jsonl&limit=2")
    assert (raw.decode() if isinstance(raw, bytes) else raw).count("\n") == 2
    assert http_request(H, "GET", "/api/audit?format=xml")[0] == 400
    assert http_request(H, "GET", "/api/audit?before_id=x")[0] == 400
    db.close()


def test_metrics_endpoint(tmp_path: Path) -> None:
    from taskqueue.metrics_export import render_metrics

    H, db, _ = _env(tmp_path)
    code, raw = http_request(H, "GET", "/api/metrics")
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert code == 200 and "# TYPE tq_tasks gauge" in text and "# TYPE tq_workers gauge" in text
    out = render_metrics(db)
    assert out.endswith("\n") and "tq_daemon_rss_megabytes" not in out
    from taskqueue.metrics_export import _sample

    bs = chr(92)
    assert _sample('tq_x', 1, a='q"' + bs) == 'tq_x{a="q' + bs + '"' + bs + bs + '"} 1'
    db.close()


def test_notify_dead_letter_list_and_retry(tmp_path: Path) -> None:
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from taskqueue import notify

    H, db, _ = _env(tmp_path)
    hits: list[bytes] = []

    class _Sink(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url_ok = f"http://127.0.0.1:{srv.server_port}/hook"
        db.execute(
            "INSERT INTO notify_dead (url, payload, error, attempts, failed_at) VALUES (?,?,?,?,?)",
            (url_ok, _json.dumps({"task_id": "t1"}), "boom", 3, "2026-01-01T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO notify_dead (url, payload, error, attempts, failed_at) VALUES (?,?,?,?,?)",
            ("http://127.0.0.1:9/never", "{}", "boom", 3, "2026-01-01T00:00:00Z"),
        )
        db.commit()
        code, raw = http_request(H, "GET", "/api/notify/dead")
        dead = raw["dead"]
        assert code == 200 and len(dead) == 2 and dead[1]["payload"] == {"task_id": "t1"}

        ok_id, bad_id = dead[1]["id"], dead[0]["id"]
        code, raw = http_request(H, "POST", f"/api/notify/dead/{ok_id}/retry")
        assert code == 200 and raw["ok"] is True
        assert hits and _json.loads(hits[0]) == {"task_id": "t1"}
        code, _ = http_request(H, "POST", f"/api/notify/dead/{bad_id}/retry")
        assert code == 502
        code, _ = http_request(H, "POST", "/api/notify/dead/999/retry")
        assert code == 404
        code, _ = http_request(H, "POST", "/api/notify/dead/abc/retry")
        assert code == 400
        assert [d["id"] for d in notify.list_dead_letters(db)] == [bad_id]
    finally:
        srv.shutdown()
        db.close()


def test_run_detail_and_diff(tmp_path: Path) -> None:
    from taskqueue._db_runs import insert_run
    from taskqueue.operator_features import diff_runs

    H, db, _ = _env(tmp_path)
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, env, enqueued_at, updated_at) "
        "VALUES ('proj','t1','python','[\"--lr\",\"1\"]','{\"SECRET\":\"hunter2\",\"MODE\":\"a\"}','x','x')"
    )
    r1 = insert_run(db, "proj", "t1", "log1")
    db.execute("UPDATE tasks SET args_override = '[\"--lr\",\"2\"]', env = '{\"MODE\":\"b\"}' WHERE task_id='t1'")
    r2 = insert_run(db, "proj", "t1", "log2")
    db.execute("UPDATE runs SET status='completed', exit_code=0, metrics_json='{\"loss\": 1.5}' WHERE run_id=?", (r1,))
    db.execute("UPDATE runs SET status='failed', exit_code=1, metrics_json='{\"loss\": 2.5}' WHERE run_id=?", (r2,))
    db.commit()

    code, d = http_request(H, "GET", f"/api/runs/{r1}")
    assert code == 200 and d["args"] == ["--lr", "1"] and sorted(d["env_keys"]) == ["MODE", "SECRET"]
    assert "hunter2" not in str(d) and "log_path" not in d
    code, diff = http_request(H, "GET", f"/api/runs/diff?a={r1}&b={r2}")
    ch = diff["changes"]
    assert code == 200 and diff["same_task"] and not diff["identical"]
    assert ch["args"] == {"a": ["--lr", "1"], "b": ["--lr", "2"]}
    assert ch["env_keys"] == {"only_a": ["SECRET"], "only_b": []}
    assert ch["status"]["b"] == "failed" and ch["metrics"]["loss"] == {"a": 1.5, "b": 2.5}
    assert diff_runs(db, r1, r1)["identical"] is True
    assert http_request(H, "GET", "/api/runs/9999")[0] == 404
    assert http_request(H, "GET", f"/api/runs/diff?a={r1}&b=9999")[0] == 404
    assert http_request(H, "GET", "/api/runs/diff?a=x&b=1")[0] == 400
    db.close()


def test_cross_run_log_search(tmp_path: Path) -> None:
    from taskqueue._db_runs import insert_run

    H, db, _ = _env(tmp_path)
    for tid, text in (("a", "ok\nCUDA out of memory\n"), ("b", "all good\n")):
        db.execute(
            "INSERT INTO tasks (project, task_id, command, args, env, enqueued_at, updated_at) "
            "VALUES ('proj',?,'python','[]','{}','x','x')", (tid,))
        lp = tmp_path / f"{tid}.log"
        lp.write_text(text)
        insert_run(db, "proj", tid, str(lp))
    db.commit()
    code, d = http_request(H, "GET", "/api/log-search?q=out%20of%20memory")
    assert code == 200 and [m["task_id"] for m in d["matches"]] == ["a"]
    assert d["matches"][0]["line"] == "CUDA out of memory" and d["matches"][0]["run_id"]
    assert http_request(H, "GET", "/api/log-search?q=")[0] == 400
    code, d = http_request(H, "GET", "/api/log-search?q=memory&project=nope")
    assert code == 200 and d["matches"] == []


def test_log_incremental_fetch_from_offset(tmp_path: Path) -> None:
    from taskqueue._db_runs import insert_run

    H, db, _ = _env(tmp_path)
    db.execute(
        "INSERT INTO tasks (project, task_id, command, args, env, enqueued_at, updated_at) "
        "VALUES ('proj','a','python','[]','{}','x','x')")
    lp = tmp_path / "a.log"
    lp.write_bytes(b"one\ntwo\npart")
    rid = insert_run(db, "proj", "a", str(lp))
    db.commit()
    code, body = http_request(H, "GET", f"/api/logs/{rid}?from=4")
    assert code == 200 and body == b"two\n"  # the unfinished "part" line is held back
    code, body = http_request(H, "GET", f"/api/logs/{rid}?from=8")
    assert body == b""
    lp.write_bytes(b"one\ntwo\npart\nmore\n")
    code, body = http_request(H, "GET", f"/api/logs/{rid}?from=8")
    assert body == b"part\nmore\n"
    # offset beyond EOF (log rotated) falls back to the normal tail
    code, body = http_request(H, "GET", f"/api/logs/{rid}?from=9999")
    assert body.startswith(b"one\n")
