"""tq explain <error_code> (M7 / C-004)."""

from __future__ import annotations

from pathlib import Path

from tqdaemon.cli import _cmd_explain


def test_explain_known_code(tmp_path: Path, capsys) -> None:
    rc = _cmd_explain(tmp_path, ["tq.block.deps"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tq.block.deps:" in out
    assert "Dependency" in out


def test_explain_unknown_code(tmp_path: Path, capsys) -> None:
    rc = _cmd_explain(tmp_path, ["tq.not.a.real.code"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown" in err.lower()


def test_explain_json(tmp_path: Path, capsys) -> None:
    rc = _cmd_explain(tmp_path, ["tq.run.cap", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"code": "tq.run.cap"' in out or '"code":"tq.run.cap"' in out
    assert "known" in out
