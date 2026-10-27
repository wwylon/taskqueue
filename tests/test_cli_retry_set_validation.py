"""tq retry --set validation: dangling flag, malformed value, missing task."""
from __future__ import annotations

from pathlib import Path

from taskqueue import cli
from taskqueue._cli_task import cmd_retry
from taskqueue.db import open_db


def test_dangling_set_flag_is_an_error(tmp_path: Path, capsys) -> None:
    assert cli._cmd_retry(tmp_path, ["p/t", "--set"]) == 1
    assert "--set requires a value" in capsys.readouterr().out


def test_set_on_missing_task_fails(tmp_path: Path, capsys) -> None:
    open_db(tmp_path / "queue.db").close()
    assert cmd_retry(tmp_path, "p/nope", set_args=["--lr=1"]) == 1
    assert "task not found" in capsys.readouterr().out


def test_set_value_must_look_like_a_flag(tmp_path: Path, capsys) -> None:
    open_db(tmp_path / "queue.db").close()
    assert cmd_retry(tmp_path, "p/t", set_args=["lr=1"]) == 1
    assert cmd_retry(tmp_path, "p/t", set_args=["--=1"]) == 1
    assert "invalid --set" in capsys.readouterr().out
