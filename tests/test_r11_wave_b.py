"""Round 11 Wave B: notify redact, dispatch busy, max_attempts retry, sleep tier."""
from __future__ import annotations

from pathlib import Path

from taskqueue._db_tasks import failed_attempt_count, retry_skip_reason, retry_task
from taskqueue.db import get_task, open_db, parse_task_yaml, upsert_task
from taskqueue.notify import list_dead_letters, redact_notify_url
from taskqueue.scheduler import should_schedule_locally
from tests.test_queue import _project, _yaml


def test_f120_redact_notify_url() -> None:
    assert redact_notify_url("https://user:pass@hooks.example/x?token=abc") == (
        "https://***@hooks.example/x"
    )
    assert "?token" not in redact_notify_url("http://127.0.0.1/hook?token=secret")
    assert redact_notify_url("") == ""


def test_f120_list_dead_letters_redacts(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    db.execute(
        "INSERT INTO notify_dead (url, payload, error, attempts, failed_at) "
        "VALUES (?, ?, 'err', 1, '2020-01-01T00:00:00+00:00')",
        (
            "https://user:secret@example.com/hook?k=1",
            '{"notify_url":"https://user:secret@example.com/hook?k=1"}',
        ),
    )
    db.commit()
    rows = list_dead_letters(db, limit=10)
    assert rows and "***@" in rows[0]["url"]
    assert "secret" not in rows[0]["url"]
    assert "secret" not in str(rows[0].get("payload"))
    db.close()


def test_f123_retry_resets_max_attempts_window(tmp_path: Path) -> None:
    db = open_db(tmp_path / "queue.db")
    proj = _project("p", tmp_path)
    upsert_task(
        db,
        parse_task_yaml(
            _yaml(tmp_path, "p", "t", command="x", max_attempts=1),
            proj,
        ),
    )
    db.execute(
        "UPDATE tasks SET status='failed', max_attempts=1 WHERE project='p' AND task_id='t'"
    )
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at) "
        "VALUES ('p', 't', 'failed', '2020-01-01T00:00:00+00:00')"
    )
    db.commit()
    assert retry_skip_reason(db, "p", "t") == "max_attempts"
    assert retry_task(db, "p", "t") is True
    row = get_task(db, "p", "t")
    assert row["status"] == "queued"
    assert failed_attempt_count(db, "p", "t") == 0  # baseline advanced
    db.close()


def test_f125_remote_only_if_busy() -> None:
    assert should_schedule_locally("remote-only-if-busy", online_workers=0, local_running=5) is True
    assert should_schedule_locally("remote-only-if-busy", online_workers=2, local_running=0) is True
    assert should_schedule_locally("remote-only-if-busy", online_workers=2, local_running=3) is False
    assert should_schedule_locally("remote", online_workers=1) is False
