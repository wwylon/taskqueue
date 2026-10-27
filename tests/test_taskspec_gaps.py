from pathlib import Path

import pytest

from taskqueue.taskspec import validate_task_spec

P = Path("t.yaml")


def test_priority_null_and_text_rejected_cleanly():
    validate_task_spec({"id": "a", "command": "x", "priority": None}, P)
    with pytest.raises(ValueError, match="priority"):
        validate_task_spec({"id": "a", "command": "x", "priority": "high"}, P)


def test_transfer_must_be_mappings():
    with pytest.raises(ValueError, match="transfer"):
        validate_task_spec({"id": "a", "command": "x", "transfer": ["a/b"]}, P)


def test_drain_cursor_does_not_lose_events_beyond_limit(tmp_path):
    import json

    from taskqueue.notify import NOTIFY_LOG_NAME, drain_notifications

    lines = [json.dumps({"ts": f"2026-01-01T00:00:0{i}Z", "project": "p", "task_id": f"t{i}"}) for i in range(5)]
    (tmp_path / NOTIFY_LOG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    seen = []
    for _ in range(4):
        seen += [e["task_id"] for e in drain_notifications(tmp_path, limit=2, incremental=True)["events"]]
    assert seen == ["t0", "t1", "t2", "t3", "t4"]
