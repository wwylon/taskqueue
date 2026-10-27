"""Slice 0 — tq doctor reports home, schema, protocol, port."""

from __future__ import annotations

import pytest

from pathlib import Path

from taskqueue import __version__
from taskqueue.cli import cmd_doctor
from taskqueue.config import PROTOCOL_VERSION
from taskqueue.db import SCHEMA_VERSION, open_db, set_config


def test_protocol_version_constant() -> None:
    assert PROTOCOL_VERSION == 1


@pytest.mark.slow
def test_doctor_prints_key_fields(tmp_path: Path, capsys) -> None:
    home = tmp_path / "tq-home"
    home.mkdir()
    db = home / "queue.db"
    conn = open_db(db)
    set_config(conn, "web_port", 8766)
    conn.close()

    cmd_doctor(home)
    out = capsys.readouterr().out
    assert f"version:           {__version__}" in out
    assert f"home:              {home}" in out
    assert "web_port:          8766" in out
    assert "ui:                http://127.0.0.1:8766" in out
    assert f"protocol:          {PROTOCOL_VERSION}" in out
    assert f"schema_version:    {SCHEMA_VERSION}" in out
    assert "daemon:            not running" in out
    assert "db_size:" in out
    assert "projects:" in out
    assert "workers:" in out
    assert "daemon_log:" in out


def test_daemon_log_grep(tmp_path: Path, capsys) -> None:
    from taskqueue._cli_admin import cmd_daemon_log

    home = tmp_path / "tq-home"
    home.mkdir()
    (home / "daemon.log").write_text(
        "keep me SHADOW would-block\nother line\nreservation: holding\n",
        encoding="utf-8",
    )
    assert cmd_daemon_log(home, grep="SHADOW|reservation") == 0
    out = capsys.readouterr().out
    assert "SHADOW would-block" in out
    assert "reservation: holding" in out
    assert "other line" not in out


def test_daemon_log_missing(tmp_path: Path, capsys) -> None:
    from taskqueue._cli_admin import cmd_daemon_log

    home = tmp_path / "tq-home"
    home.mkdir()
    assert cmd_daemon_log(home) == 1
    assert "No daemon.log" in capsys.readouterr().out
