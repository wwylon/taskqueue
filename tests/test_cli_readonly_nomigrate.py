"""Read-only CLI commands must not request a migrating DB open."""
from __future__ import annotations

from pathlib import Path

import pytest

import taskqueue.db as dbmod
from taskqueue import _cli_admin, _cli_task
from taskqueue.db import open_db as real_open
from taskqueue.config import db_path as _db_path


@pytest.mark.parametrize(
    "mod,call",
    [
        (_cli_admin, lambda h: _cli_admin.cmd_health(h, None)),
        (_cli_admin, lambda h: _cli_admin.cmd_attention(h, None)),
        (_cli_admin, lambda h: _cli_admin.cmd_my_tasks(h, "op", None)),
        (_cli_admin, lambda h: _cli_admin.cmd_report(h, None, 7)),
        (_cli_task, lambda h: _cli_task.cmd_describe(h, "nope/none")),
    ],
)
def test_readonly_commands_open_without_migrate(tmp_path: Path, monkeypatch, mod, call) -> None:
    real_open(_db_path(tmp_path)).close()
    seen: list[bool] = []

    def spy(path, *, migrate=True):
        seen.append(migrate)
        return real_open(path, migrate=migrate)

    monkeypatch.setattr(dbmod, "open_db", spy)
    try:
        call(tmp_path)
    except SystemExit:
        pass
    assert seen and all(m is False for m in seen)
