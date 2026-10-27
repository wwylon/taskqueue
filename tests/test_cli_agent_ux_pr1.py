"""PR1 CLI agent UX: list enabled/progress, result, tail --last, patch WARN."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from taskqueue._cli_helpers import last_nonempty_line, read_last_lines, warn_enabled_but_terminal
from taskqueue._cli_task import cmd_list, cmd_result, cmd_tail
from taskqueue.db import open_db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seed_task(
    conn,
    *,
    project: str = "p",
    task_id: str = "t1",
    status: str = "completed",
    enabled: int = 1,
    progress_pct: float | None = None,
    last_log_line: str | None = None,
    log_path: str | None = None,
) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, enabled, priority, "
        "resources, depends_on, exclusive, max_attempts, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, 'echo', '[]', ?, 50, '{}', '[]', 0, 0, ?, ?)",
        (project, task_id, status, enabled, now, now),
    )
    if log_path is not None or progress_pct is not None or last_log_line is not None:
        cur = conn.execute(
            "INSERT INTO runs (project, task_id, started_at, finished_at, status, log_path, "
            "progress_pct, last_log_line, exit_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project,
                task_id,
                now,
                now,
                status,
                log_path,
                progress_pct,
                last_log_line,
                0 if status == "completed" else 1,
            ),
        )
        conn.execute(
            "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?",
            (cur.lastrowid, project, task_id),
        )
    conn.commit()


def test_last_nonempty_and_read_last_lines(tmp_path: Path) -> None:
    p = tmp_path / "log.txt"
    p.write_text("a\nb\n\nc\n\n", encoding="utf-8")
    assert last_nonempty_line(p) == "c"
    assert read_last_lines(p, 2) == ["c", ""]
    assert read_last_lines(p, 5) == ["a", "b", "", "c", ""]


def test_list_json_includes_enabled_and_progress(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = home / "queue.db"
    conn = open_db(db)
    _seed_task(
        conn,
        status="running",
        enabled=1,
        progress_pct=42.5,
        last_log_line="(42.5%) deal 10/20",
        log_path=str(tmp_path / "r.log"),
    )
    _seed_task(
        conn,
        task_id="t2",
        status="completed",
        enabled=0,
        progress_pct=100.0,
        log_path=str(tmp_path / "r2.log"),
    )
    conn.close()
    monkeypatch.setattr("taskqueue._cli_task._db", lambda h: db)
    monkeypatch.setattr("taskqueue.config.load_projects", lambda h: {})
    rc = cmd_list(home, None, as_json=True)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    by_id = {t["task_id"]: t for t in out}
    assert by_id["t1"]["enabled"] is True
    assert by_id["t1"]["progress_pct"] == 42.5
    assert by_id["t2"]["enabled"] is False
    assert by_id["t2"]["status"] == "completed"


def test_list_human_yaml_off_and_progress(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = home / "queue.db"
    conn = open_db(db)
    _seed_task(conn, status="completed", enabled=0, log_path=str(tmp_path / "a.log"))
    _seed_task(
        conn,
        task_id="run",
        status="running",
        enabled=1,
        progress_pct=10.0,
        log_path=str(tmp_path / "b.log"),
    )
    conn.close()
    monkeypatch.setattr("taskqueue._cli_task._db", lambda h: db)
    rc = cmd_list(home, None, as_json=False)
    assert rc == 0
    text = capsys.readouterr().out
    assert "[yaml-off]" in text
    assert "10%" in text


def test_cmd_result_exit_codes(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = home / "queue.db"
    log = tmp_path / "out.log"
    log.write_text("noise\n{\"ok\": true}\n\n", encoding="utf-8")
    conn = open_db(db)
    _seed_task(conn, status="completed", log_path=str(log))
    conn.close()
    monkeypatch.setattr("taskqueue._cli_task._db", lambda h: db)
    assert cmd_result(home, "p/t1") == 0
    assert capsys.readouterr().out.strip() == '{"ok": true}'

    conn = open_db(db)
    conn.execute("UPDATE tasks SET status='failed' WHERE project='p' AND task_id='t1'")
    conn.commit()
    conn.close()
    assert cmd_result(home, "p/t1") == 1
    assert capsys.readouterr().out.strip() == '{"ok": true}'


def test_cmd_tail_last_exits(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = home / "queue.db"
    log = tmp_path / "out.log"
    log.write_text("l1\nl2\nl3\n", encoding="utf-8")
    conn = open_db(db)
    _seed_task(conn, status="completed", log_path=str(log))
    conn.close()
    monkeypatch.setattr("taskqueue._cli_task._db", lambda h: db)
    assert cmd_tail(home, "p/t1", last_n=2) == 0
    assert capsys.readouterr().out.strip().splitlines() == ["l2", "l3"]


def test_warn_enabled_but_terminal(capsys) -> None:
    warn_enabled_but_terminal("proj", "foo", "completed")
    err = capsys.readouterr().err
    assert "[WARN]" in err
    assert "tq retry proj/foo" in err
    warn_enabled_but_terminal("proj", "foo", "queued")
    assert capsys.readouterr().err == ""


def test_warn_enabled_terminal_summary_skips_completed(tmp_path, capsys) -> None:
    from datetime import UTC, datetime

    from taskqueue._cli_helpers import warn_enabled_terminal_summary
    from taskqueue.db import open_db

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    for tid, status, enabled in (
        ("done", "completed", 1),
        ("bad", "failed", 1),
        ("off", "failed", 0),
    ):
        db.execute(
            "INSERT INTO tasks (project, task_id, command, status, enabled, "
            "enqueued_at, updated_at) VALUES ('p', ?, 'x', ?, ?, ?, ?)",
            (tid, status, enabled, now, now),
        )
    db.commit()
    warn_enabled_terminal_summary(db)
    err = capsys.readouterr().err
    assert "1 task(s) enabled but need tq retry" in err
    assert "p/bad(failed)" in err
    assert "done" not in err
    db.close()
