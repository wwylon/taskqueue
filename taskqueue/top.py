"""`tq top`: live terminal view of running tasks, fed by the daemon's /api/events stream.

Unlike `tq watch` (polls the DB every N seconds) this redraws when the daemon pushes an
update, so progress and transitions appear as they happen.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, Any]]:
    """Yield (event, data) for each complete SSE frame; comments and retry hints are skipped."""
    event, data = "message", []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                try:
                    yield event, json.loads(payload)
                except ValueError:
                    yield event, payload
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        key, _, val = line.partition(":")
        val = val[1:] if val.startswith(" ") else val
        if key == "event":
            event = val
        elif key == "data":
            data.append(val)


def _dur(sec: Any) -> str:
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return "-"
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def render(status: dict[str, Any], *, width: int = 100) -> str:
    running = status.get("running") or []
    head = f"tq top  {time.strftime('%H:%M:%S')}  running {len(running)}  queued {status.get('queued_count', 0)}"
    rows = [head, "", f"{'TASK':<44} {'PROG':>6} {'ELAPSED':>8} {'RAM GiB':>8} {'CPU%':>6}  WORKER"]
    for t in sorted(running, key=lambda r: (str(r.get("project")), str(r.get("task_id")))):
        name = f"{t.get('project')}/{t.get('task_id')}"[:44]
        pct = t.get("progress_pct")
        rows.append(
            f"{name:<44} {(f'{float(pct):.0f}%' if pct is not None else '-'):>6} {_dur(t.get('elapsed_sec')):>8} "
            f"{float(t.get('current_ram_gib') or t.get('peak_ram_gib') or 0):>8.2f} "
            f"{float(t.get('current_cpu_pct') or 0):>6.0f}  {t.get('worker_id') or 'master'}"
        )
        line = str(t.get("last_log_line") or "").strip()
        if line:
            rows.append("    " + line[: width - 4])
    if not running:
        rows.append("(nothing running)")
    return "\n".join(rows)


def run_top(home: Path, *, once: bool = False, as_json: bool = False, out=None) -> int:
    """Stream /api/events and redraw on every update. ``once`` renders the first frame and returns.

    ``as_json`` writes each update as one compact JSON line (NDJSON) instead of a frame.
    """
    import urllib.request

    from taskqueue.master_client import _auth_headers, master_url

    out = out or sys.stdout
    req = urllib.request.Request(master_url(home) + "/api/events", headers=_auth_headers(home))
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except OSError as exc:
        print(f"daemon not reachable: {exc}", file=sys.stderr)
        return 3
    try:
        lines = (b.decode("utf-8", "replace") for b in resp)
        for event, data in iter_sse(lines):
            if event not in ("message", "snapshot") or not isinstance(data, dict):
                continue
            if as_json:
                out.write(json.dumps(data, separators=(",", ":"), default=str) + "\n")
                out.flush()
                if once:
                    return 0
                continue
            frame = render(data)
            if once:
                print(frame, file=out)
                return 0
            out.write("\x1b[2J\x1b[H" + frame + "\n")
            out.flush()
    except KeyboardInterrupt:
        return 0
    finally:
        resp.close()
    return 0
