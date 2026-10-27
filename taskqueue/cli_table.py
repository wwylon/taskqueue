"""Command table for `tq`: one source for per-command help, known flags and shell completion.

Built from the usage lines in ``taskqueue.cli.__doc__`` (``  tq <cmd> ... Description``), so a
command documented there is automatically covered by ``tq <cmd> --help`` and ``tq completion``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_USAGE_RE = re.compile(r"^\s+tq ([a-z][a-z0-9-]*)(?:\|([a-z][a-z0-9-|]*))?\b(.*)$")
_FLAG_RE = re.compile(r"--[a-z0-9][a-z0-9-]*")


@dataclass
class CommandSpec:
    name: str
    usage: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    subcommands: set[str] = field(default_factory=set)


def build_table(doc: str, commands: dict) -> dict[str, CommandSpec]:
    """Every registered command, with the usage lines, flags and subcommands its help text names."""
    table = {name: CommandSpec(name) for name in commands}
    for line in (doc or "").splitlines():
        m = _USAGE_RE.match(line)
        if not m:
            continue
        names = [m.group(1), *((m.group(2) or "").split("|") if m.group(2) else [])]
        rest = m.group(3)
        for n in names:
            spec = table.get(n)
            if spec is None:
                continue
            spec.usage.append(line.strip())
            spec.flags.update(_FLAG_RE.findall(rest))
            first = rest.split()[0] if rest.split() else ""
            if re.fullmatch(r"[a-z][a-z0-9-]*", first):  # e.g. `tq worker setup`, `tq token create`
                spec.subcommands.add(first)
    return table


def completion_script(shell: str, table: dict[str, CommandSpec]) -> str:
    """Completion for bash, zsh or pwsh: commands, then that command's subcommands and flags."""
    cmds = " ".join(sorted(table))
    per = {n: " ".join(sorted(s.subcommands | s.flags | {"--help"})) for n, s in sorted(table.items())}
    if shell == "bash" or shell == "zsh":
        cases = "\n".join(f'    {n}) words="{w}";;' for n, w in per.items())
        body = f'''_tq_complete() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" words
  if [ "$COMP_CWORD" -eq 1 ]; then
    words="{cmds}"
  else
    case "${{COMP_WORDS[1]}}" in
{cases}
    *) words="--help";;
    esac
  fi
  COMPREPLY=( $(compgen -W "$words" -- "$cur") )
}}
complete -F _tq_complete tq
'''
        if shell == "zsh":
            body = "autoload -U +X bashcompinit && bashcompinit\n" + body
        return body
    if shell == "pwsh":
        entries = "\n".join(f"    '{n}' = '{w}'" for n, w in per.items())
        return f'''$tqWords = @{{
{entries}
}}
Register-ArgumentCompleter -Native -CommandName tq -ScriptBlock {{
  param($wordToComplete, $commandAst, $cursorPosition)
  $parts = $commandAst.CommandElements | ForEach-Object {{ $_.ToString() }}
  if ($parts.Count -le 1 -or ($parts.Count -eq 2 -and $wordToComplete)) {{
    $cands = '{cmds}'.Split(' ')
  }} else {{
    $cands = ($tqWords[$parts[1]] -as [string]).Split(' ')
  }}
  $cands | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
    [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
  }}
}}
'''
    raise ValueError(f"unsupported shell: {shell!r} (bash, zsh, pwsh)")
