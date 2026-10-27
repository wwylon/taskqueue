"""Command table: help per command, completion scripts generated from it."""
from __future__ import annotations

from taskqueue import cli
from taskqueue.cli_table import build_table, completion_script


def test_every_command_has_a_spec_and_most_have_usage():
    table = build_table(cli.__doc__, cli.COMMANDS)
    assert set(table) == set(cli.COMMANDS)
    assert "--force" in table["restart"].flags
    assert {"setup"} <= table["worker"].subcommands
    undocumented = sorted(n for n, s in table.items() if not s.usage)
    assert len(undocumented) <= 10, undocumented  # help text should name nearly every command


def test_completion_scripts_list_commands_and_flags(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    for shell in ("bash", "zsh", "pwsh"):
        assert cli.main(["completion", shell]) == 0
        out = capsys.readouterr().out
        assert "restart" in out and "--force" in out and "completion" in out
    assert cli.main(["completion", "fish"]) == 2
    table = build_table(cli.__doc__, cli.COMMANDS)
    assert "complete -F _tq_complete tq" in completion_script("bash", table)
