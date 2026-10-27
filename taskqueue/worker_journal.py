"""Append-only per-run journal for deterministic recovery after worker crash.

Journal lives at {home}/runs/{run_id}/journal.jsonl.

Entry types:
  {"t": "claim",   "run_id": ..., "task_id": ..., "session": ...}
  {"t": "start",   "pid": ...,    "started_at": ...}
  {"t": "exit",    "code": ...,   "finished_at": ...}
  {"t": "finalize","payload": {...}}

On restart:
- exit + no finalize → replay finalize
- no exit + claim → crashed mid-run; queue as unknown
- finalize present → GC
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def journal_dir(home: Path, run_id: int) -> Path:
    return Path(home) / "runs" / str(run_id)


def journal_path(home: Path, run_id: int) -> Path:
    return journal_dir(home, run_id) / "journal.jsonl"


def append_entry(home: Path, run_id: int, entry: dict[str, Any]) -> None:
    """Append one entry to the run journal (best-effort; never raises)."""
    try:
        p = journal_path(home, run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def read_journal(home: Path, run_id: int) -> list[dict[str, Any]]:
    """Read all valid entries from a run journal."""
    p = journal_path(home, run_id)
    entries: list[dict[str, Any]] = []
    if not p.is_file():
        return entries
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return entries


def gc_journal(home: Path, run_id: int) -> None:
    """Delete the journal directory for a completed run."""
    try:
        d = journal_dir(home, run_id)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def scan_unfinished(home: Path) -> list[tuple[int, str]]:
    """Scan all run journals and return (run_id, state) for unfinished runs.

    state: 'replay_finalize' (exit but no finalize) or 'unknown' (no exit).
    """
    results = []
    runs_dir = Path(home) / "runs"
    if not runs_dir.is_dir():
        return results
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        try:
            run_id = int(run_dir.name)
        except ValueError:
            continue
        entries = read_journal(home, run_id)
        t_types = {e.get("t") for e in entries}
        if "finalize" in t_types:
            gc_journal(home, run_id)
            continue
        if "exit" in t_types:
            results.append((run_id, "replay_finalize"))
        elif "claim" in t_types:
            results.append((run_id, "unknown"))
    return results
