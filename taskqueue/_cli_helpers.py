"""Private CLI helper functions shared across command modules."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# CLI exit-code contract: 0 ok, 1 error, 2 usage, 3 master unreachable when the command needs it.
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3


def emit_json(data: object, *, indent: int | None = 2, ensure_ascii: bool = True) -> None:
    """Print *data* as JSON on stdout - the one place every ``--json`` flag goes through.

    ``default=str`` keeps Paths / datetimes from crashing a machine-readable run.
    ``indent=None`` gives a compact single line (NDJSON streams).
    """
    print(json.dumps(data, indent=indent, ensure_ascii=ensure_ascii, default=str))


def _home() -> Path:
    from taskqueue.config import state_home
    return state_home()


def _db(home: Path) -> Path:
    from taskqueue.config import db_path
    return db_path(home)


def _pid_path(home: Path) -> Path:
    from taskqueue.config import pid_path
    return pid_path(home)


def _daemon_running(home: Path) -> int | None:
    pid_file = _pid_path(home)
    if not pid_file.exists():
        return None
    try:
        raw = pid_file.read_text().strip()
        import psutil
        if ":" in raw:
            # F-103: PID:create_time format written since 0.9+
            pid_str, ct_str = raw.split(":", 1)
            pid = int(pid_str)
            expected_ct = float(ct_str)
            if not psutil.pid_exists(pid):
                return None
            actual_ct = psutil.Process(pid).create_time()
            if abs(actual_ct - expected_ct) > 1.0:
                return None  # PID reused by an unrelated process
        else:
            pid = int(raw)
            if not psutil.pid_exists(pid):
                return None
        return pid
    except Exception:
        pass
    return None


def _web_port(home: Path) -> int:
    """Read configured web_port; default 8765 if DB missing."""
    from taskqueue.db import read_config

    try:
        return int(read_config(home).get("web_port", 8765))
    except (TypeError, ValueError):
        return 8765


def _ui_url(home: Path) -> str:
    from taskqueue.db import read_config

    host = "127.0.0.1"
    cfg_host = str(read_config(home).get("bind_host") or "").strip()
    if cfg_host and cfg_host != "0.0.0.0":
        host = cfg_host
    return f"http://{host}:{_web_port(home)}"


def _ensure_python() -> Path:
    exe = Path(sys.executable)
    # On Windows, use pythonw.exe (GUI subsystem) to avoid console flash when
    # spawning the daemon as a detached process. python.exe is console-subsystem
    # and briefly flickers a terminal even with CREATE_NO_WINDOW.
    if os.name == "nt" and exe.stem.lower() == "python":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    if exe.exists():
        return exe
    raise SystemExit("Could not find Python executable.")


def _split_key(key: str) -> tuple[str, str]:
    project, sep, task_id = key.partition("/")
    if not sep or not project or not task_id:
        raise SystemExit(f"Expected 'project/task_id', got: {key!r}")
    return project, task_id


# Statuses where enabled=true does not admit a new run (need tq retry).
_ENABLED_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "interrupted", "stopped", "suspended", "unknown", "cancelled"}
)

# Post-sync summary: only statuses that usually need operator action.
# completed+enabled is the normal steady state for finished experiments.
_ENABLED_NEEDS_RETRY_STATUSES = frozenset(
    {"failed", "interrupted", "stopped", "suspended", "unknown", "cancelled"}
)


def warn_enabled_but_terminal(project: str, task_id: str, status: str) -> None:
    """Stderr WARN when enabling a task that will not run until retry."""
    if status not in _ENABLED_TERMINAL_STATUSES:
        return
    print(
        f"[WARN] {project}/{task_id} is enabled but status={status} — "
        f"will not run until: tq retry {project}/{task_id}",
        file=sys.stderr,
    )


def warn_enabled_terminal_summary(
    conn,
    *,
    project: str | None = None,
    limit: int = 5,
) -> None:
    """After sync: warn only for enabled tasks that still need ``tq retry``.

    Skips ``completed`` — leaving finished YAMLs ``enabled: true`` is normal.
    """
    statuses = _ENABLED_NEEDS_RETRY_STATUSES
    clauses = ["enabled = 1", f"status IN ({','.join('?' * len(statuses))})"]
    params: list = list(sorted(statuses))
    if project:
        clauses.append("project = ?")
        params.append(project)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT project, task_id, status FROM tasks WHERE {where} "
        f"ORDER BY project, task_id LIMIT ?",
        (*params, limit),
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks WHERE {where}",
        params,
    ).fetchone()["n"]
    if not total:
        return
    examples = ", ".join(
        f"{r['project']}/{r['task_id']}({r['status']})" for r in rows
    )
    extra = f" (+{total - len(rows)} more)" if total > len(rows) else ""
    print(
        f"[WARN] {total} task(s) enabled but need tq retry "
        f"(failed/interrupted/stopped/...); e.g. {examples}{extra}",
        file=sys.stderr,
    )


def _check_ownership_or_print(row, key: str, *, force: bool) -> str | None:
    """Check task ownership; print error or force-override notice.

    Returns an error string if the caller should abort (return 1), else None.
    """
    import os
    from taskqueue.db import check_ownership
    operator = os.environ.get("TQ_OPERATOR")
    started_by = row["started_by"] if row and "started_by" in row.keys() else None
    err = check_ownership(started_by, operator, force=force)
    if err:
        print(f"Error: {key} {err}")
        return err
    if force and started_by and started_by != operator:
        print(f"[force] overriding ownership: {key} started_by='{started_by}', you are '{operator}'")
    return None


def format_resources(res: dict) -> str:
    """Format a task resources dict as a compact display string."""
    cpu = res.get("cpu_cores", 1)
    ram = res.get("ram_gib", 0)
    try:
        gpu = float(res.get("gpu_vram_gib") or 0)
    except (TypeError, ValueError):
        gpu = 0.0
    return f"cpu={cpu} ram={ram}G" + (f" GPU {gpu:g}G" if gpu > 0 else "")


def read_last_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of a text file via bounded byte-tail."""
    from taskqueue.logio import tail_log_lines

    if n <= 0:
        return []
    lines = tail_log_lines(path, n)
    if len(lines) == 1 and lines[0].startswith("("):
        return []
    return lines


def last_nonempty_line(path: Path) -> str | None:
    """Last non-empty line of a log file, or None if none."""
    from taskqueue.logio import tail_log_lines

    for line in reversed(tail_log_lines(path, 200)):
        if line.startswith("("):
            continue
        if line.strip():
            return line
    return None


def master_mutation(home: Path, path: str, *, force: bool = False, params: dict | None = None) -> tuple[dict | None, str]:
    """POST a write to the master when a daemon runs, so the CLI never writes SQLite under it.

    Returns (response, mode): mode "master" (response is the JSON body), "unreachable"
    (a daemon runs but did not answer: the caller must not write SQLite; exit 3), or
    "offline" (no daemon: the caller may write SQLite directly).
    """
    from urllib.parse import urlencode

    from taskqueue.master_client import via_master

    q = {}
    if os.environ.get("TQ_OPERATOR"):
        q["operator"] = os.environ["TQ_OPERATOR"]
    if force:
        q["force"] = "1"
    q.update({k: str(v) for k, v in (params or {}).items() if v is not None})
    resp = via_master(home, "POST", path + (("?" + urlencode(q)) if q else ""))
    if resp is not None:
        return resp, "master"
    if _daemon_running(home) is not None:
        print("[ERROR] daemon is running but did not answer; refusing a direct SQLite write.", file=sys.stderr)
        return None, "unreachable"
    return None, "offline"

