"""tq — shared task queue CLI.

Daemon
  tq start                          Start the daemon (background, detached)
  tq stop-daemon                    Ask daemon to shut down gracefully
  tq restart [--force]              Stop (wait for exit) then start; refuses while tasks run
  tq doctor [--json] [--torch]                         Version, home, uptime, DB size, projects, memory, port
  tq log [--bytes N] [--grep PAT]   Tail daemon.log (scheduler stdout; not task logs)
  tq detect                         Detect host hardware
  tq master-status                  Who holds master_lease
  tq master-migrate <url>           Live migrate to a standby master

Inspect queue
  tq status [--project P] [--json]  Running + recent runs
  tq list [--project P] [-v] [--tag T] [--suite S]  Full queue table
  tq describe <project/task_id>     Full details for one task
  tq top [--once] [--json]         Live running-task view pushed by the daemon (SSE); --json = NDJSON
  tq watch [--interval SEC]         Live refresh of status
  tq wait <project/task_id ...|--suite S|--group G|--tag T|--mine> [--any] [--timeout SEC] [--json]
                                   Block until tasks settle (exit 0 ok, 1 failed, 2 timeout, 3 not found)
  tq my-tasks [--operator NAME]     Tasks for TQ_OPERATOR / --operator
  tq agents [--days N] [--by dims] [--task-by dims] [--json]
  tq health [--line] [--plain] [--json] [--ack]   Red/amber/green queue health (status lines, alerts)
                                   Usage analytics: who calls what, task provenance and outcomes
  tq capacity                       Cluster free/total CPU RAM GPU
  tq plan [N] [--json]              Dry-run: which queued tasks would start next (approximate)
  tq fair-share                     Decayed GPU fair-share debt
  tq report [--project P] [--days N] Aggregate runs / error codes

Tasks
  tq sync [PROJECT ...] [--force] [--require-daemon]  Re-sync registered project YAML dirs (exit 3 offline)
  tq archive [PROJECT ...] [--older-than 30d] [--status completed,failed] [--dry-run]
                                    Export old finished tasks to <home>/archive/*.jsonl and drop them from the queue
  tq add <yaml> --project P         Enqueue from a YAML file (via master HTTP)
  tq submit <yaml> --project P      Alias for add
  tq validate [--fix] [path.yaml]  Lint YAMLs; --fix rewrites legacy keys in place
  tq stop <project/task_id> [--force]  Graceful stop (checkpoints first)
  tq stop --project P               Stop all running tasks in project P
  tq suspend <project/task_id>      Cooperative suspend (exit 75)
  tq retry <project/task_id> [--downstream] [--enable-downstream]
                                    Re-queue (+ dependents; optional enable-then-retry)
  tq retry --project P              Retry all terminal-retryable in P
  tq reverify <project/root> --up-to <task_id>
                                    Enable root→target, disable other downstream, retry
  tq locks [--json]                 Active resource locks, holders, waiters
  tq force <project/task_id>        Bypass resource gates once
  tq remove <project/task_id> [--force]  Unregister from DB
  tq remove --project P [--force]        Purge all tasks+runs for project P from DB
  tq patch <project/task_id> k=v…   Patch priority|enabled|wall_clock_cap_sec|max_attempts
  tq clone <project/task_id> <new_id> [--set k=v]  New task YAML copied from an existing task
  tq diff <run_a> <run_b> [--json] Compare two runs (status, exit, duration, peak RAM/VRAM/CPU)
  tq why <project/task_id> [--json] Why a task is (not) running: block reason, deps, pause, dry-run verdict
  tq priority <project/task_id> <N> [--force]  Live priority (higher = sooner)
  tq logs <project/task_id> [--bytes N] [--grep PAT] [--jsonl] [-f]  Tail task log (-f follows until it settles; exit codes as `tq wait`)
  tq result <project/task_id>       Last non-empty log line (exit 0 iff completed)
  tq tail <project/task_id> [--last N]  Follow log, or dump last N lines and exit
  tq diagnose <project/task_id>     Task + runs + log tail + worker bundle
  tq deps <project/task_id> [--mermaid]  Dependency tree
  tq tag add|rm <project/task_id> <tags…>
  tq group list|stop|retry <project> <group>
  tq metrics <project/task_id> [--json] [--samples N]  UI/MCP progress payload

Projects
  tq projects [--validate]          List registered projects
  tq resync <project> [--worker ID] Force allowlisted file re-sync to workers
  tq pause|resume <project>         Pause / resume scheduling
  tq freeze|unfreeze <project>      Freeze / unfreeze project
  tq suite list|…                   Named suites
  tq bundle <project>               Create a project bundle

Workers / power
  tq worker list                    Registered workers
  tq worker remove <worker_id>      Deregister a worker
  tq worker build-wheel             Wheel for worker auto-upgrade
  tq worker config-master --host H  Emit join YAML for a worker (secret)
  tq worker setup --from <join.yaml>  Bootstrap worker from join file
  tq worker setup --master <url>    Clone projects + venvs on a worker
  tq worker --master <url> --worker-id <id>  Start worker agent
  tq worker sync <worker_id>        Queue git pull + uv sync on worker
  tq sleep|wake <key> [--project P] Host sleep / wake helpers

Config / maintenance
  tq config get|set k=v…            Resource limits and knobs
  tq migrate priority-invert [--dry-run]  Remap priorities 100-N (once per home)
  tq completion bash|zsh|pwsh       Print a shell completion script (e.g. tq completion bash >> ~/.bashrc)
  tq prune [--apply] [--verbose]    Delete old logs/runs (dry-run default)
  tq export [path.tar.gz] [--with-logs]
  tq import <path.tar.gz> [--force]
  tq token generate                 Create and store auth_token
  tq usage                          CLI command usage counts
  tq mcp                            Stdio MCP server

Notes
  --force on stop/remove/priority overrides started_by vs TQ_OPERATOR ownership.
  Daemon stdout is {TASKQUEUE_HOME}/daemon.log — use `tq log`, not `tq logs`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from taskqueue._cli_helpers import (  # noqa: F401
    emit_json,
    _home, _db, _pid_path, _daemon_running, _web_port, _ui_url, _ensure_python, _split_key,
)
from taskqueue._cli_task import *     # noqa: F401,F403
from taskqueue._cli_project import *  # noqa: F401,F403
from taskqueue._cli_worker import *   # noqa: F401,F403
from taskqueue._cli_admin import *    # noqa: F401,F403


# ── arg helpers (shared by command wrappers) ─────────────────────────────────

def _pop_project(lst: list[str]) -> tuple[str | None, list[str]]:
    for i, a in enumerate(lst):
        if a == "--project" and i + 1 < len(lst):
            return lst[i + 1], lst[:i] + lst[i + 2:]
        if a.startswith("--project="):
            return a.split("=", 1)[1], lst[:i] + lst[i + 1:]
    return None, lst


def _pop_flag(lst: list[str], flag: str) -> tuple[bool, list[str]]:
    if flag in lst:
        i = lst.index(flag)
        return True, lst[:i] + lst[i + 1:]
    return False, lst


def _pop_value(lst: list[str], flag: str) -> tuple[str | None, list[str]]:
    for i, a in enumerate(lst):
        if a == flag and i + 1 < len(lst):
            return lst[i + 1], lst[:i] + lst[i + 2:]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1], lst[:i] + lst[i + 1:]
    return None, lst


def _parse_kv_flags(rest: list[str], spec: dict) -> tuple[dict, list[str]]:
    """Parse multiple --flag / --flag=VAL arguments in one pass.

    spec: {flag_name: converter_or_None}  (None keeps the raw string)
    Returns (values, remaining_args).  Flags absent from rest are not in the dict.
    """
    vals: dict = {}
    remaining = list(rest)
    for flag, conv in spec.items():
        raw, remaining = _pop_value(remaining, flag)
        if raw is not None:
            vals[flag] = conv(raw) if conv is not None else raw
    return vals, remaining


# ── thin command wrappers: (home, rest) -> int ───────────────────────────────

def _reject_args(name: str, rest: list[str]) -> int | None:
    """Usage error (exit 2) for a command that takes no arguments, so `tq start --help` cannot act."""
    if rest:
        print(f"Usage: tq {name}   (takes no arguments; got: {' '.join(rest)})")
        return 2
    return None


def _cmd_start(home: Path, rest: list[str]) -> int:
    bad = _reject_args("start", rest)
    if bad is not None:
        return bad
    return cmd_start(home) or 0


def _cmd_stop_daemon(home: Path, rest: list[str]) -> int:
    force = "--force" in rest
    bad = _reject_args("stop-daemon [--force]", [a for a in rest if a != "--force"])
    if bad is not None:
        return bad
    return cmd_stop_daemon(home, force=force) or 0


def _cmd_restart(home: Path, rest: list[str]) -> int:
    timeout = 180.0
    force = "--force" in rest
    rest2 = [a for a in rest if a != "--force"]
    if rest2 and rest2[0].startswith("--timeout="):
        try:
            timeout = float(rest2[0].split("=", 1)[1])
        except ValueError:
            print("Usage: tq restart [--timeout=SEC] [--force]")
            return 1
        rest2 = rest2[1:]
    elif rest2 and rest2[0] == "--timeout" and len(rest2) > 1:
        try:
            timeout = float(rest2[1])
        except ValueError:
            print("Usage: tq restart [--timeout=SEC] [--force]")
            return 1
        rest2 = rest2[2:]
    if rest2:
        print("Usage: tq restart [--timeout=SEC] [--force]")
        return 1
    return cmd_restart(home, timeout_sec=timeout, force=force) or 0


def _cmd_status(home: Path, rest: list[str]) -> int:
    project, rest = _pop_project(rest)
    as_json, rest = _pop_flag(rest, "--json")
    return cmd_status(home, project, as_json=as_json)


def _cmd_list(home: Path, rest: list[str]) -> int:
    project, rest = _pop_project(rest)
    verbose, rest = _pop_flag(rest, "--verbose")
    if not verbose:
        verbose, rest = _pop_flag(rest, "-v")
    as_json, rest = _pop_flag(rest, "--json")
    artifacts, rest = _pop_flag(rest, "--artifacts")

    # Collect all --tag values (may appear multiple times).
    tags_filter: list[str] = []
    while True:
        tag, rest = _pop_value(rest, "--tag")
        if tag is None:
            break
        tags_filter.append(tag)

    kv, rest = _parse_kv_flags(rest, {
        "--suite": None, "--status": None, "--worker": None,
        "--limit": None, "--offset": None,
    })
    if rest:
        print(f"Unknown argument: {rest[0]}")
        return 1

    suite = kv.get("--suite")
    status_filter = kv.get("--status")
    worker_filter = kv.get("--worker")

    limit: int | None = None
    if "--limit" in kv:
        try:
            limit = int(kv["--limit"])
        except ValueError:
            print("Usage: tq list --limit N (N must be an integer)")
            return 1

    offset = 0
    if "--offset" in kv:
        try:
            offset = int(kv["--offset"])
        except ValueError:
            print("Usage: tq list --offset N (N must be an integer)")
            return 1

    return cmd_list(
        home, project,
        tags=tags_filter or None, suite=suite,
        status_filter=status_filter, worker_filter=worker_filter,
        verbose=verbose, as_json=as_json,
        limit=limit, offset=offset, artifacts=artifacts,
    )


def _cmd_describe(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq describe <project/task_id>")
        return 1
    return cmd_describe(home, rest[0])


def _cmd_sync(home: Path, rest: list[str]) -> int:
    require_daemon, rest = _pop_flag(rest, "--require-daemon")
    force, rest = _pop_flag(rest, "--force")
    # README spelling: ``tq sync --project NAME`` is the same as ``tq sync NAME``.
    flat: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--project" and i + 1 < len(rest):
            flat.append(rest[i + 1])
            i += 2
            continue
        flat.append(rest[i])
        i += 1
    rest = flat
    bad = [a for a in rest if a.startswith("-")]
    if bad:
        print(f"Unknown option(s): {' '.join(bad)}")
        print("Usage: tq sync [PROJECT ...] [--force] [--require-daemon]")
        return 1
    return cmd_sync(home, require_daemon=require_daemon, projects=rest or None, force=force)


def _cmd_archive(home: Path, rest: list[str]) -> int:
    from taskqueue._cli_project import cmd_archive

    usage = "Usage: tq archive [PROJECT ...] [--older-than 30d] [--status completed,failed] [--dry-run]"
    dry_run, rest = _pop_flag(rest, "--dry-run")
    days = 30.0
    statuses: list[str] | None = None
    projects: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--older-than", "--status", "--project") and i + 1 < len(rest):
            v = rest[i + 1]
            i += 2
            if a == "--project":
                projects.append(v)
            elif a == "--status":
                statuses = [x for x in v.split(",") if x]
            else:
                try:
                    days = float(v[:-1]) if v.endswith("d") else float(v)
                except ValueError:
                    print(usage)
                    return 1
            continue
        if a.startswith("-"):
            print(usage)
            return 1
        projects.append(a)
        i += 1
    return cmd_archive(
        home, projects=projects or None, older_than_days=days, statuses=statuses, dry_run=dry_run
    )


def _cmd_retry(home: Path, rest: list[str]) -> int:
    # R2: collect --set flags before project-popping
    set_args: list[str] = []
    rest_no_set: list[str] = []
    downstream = False
    enable_downstream = False
    status_filter: str | None = None
    i = 0
    while i < len(rest):
        if rest[i] == "--set" and i + 1 >= len(rest):
            print("Error: --set requires a value (--flag or --flag=value)")
            return 1
        if rest[i] == "--set" and i + 1 < len(rest):
            set_args.append(rest[i + 1])
            i += 2
        elif rest[i].startswith("--set="):
            set_args.append(rest[i][len("--set="):])
            i += 1
        elif rest[i] in ("--downstream", "-d"):
            downstream = True
            i += 1
        elif rest[i] == "--enable-downstream":
            enable_downstream = True
            i += 1
        elif rest[i] == "--status" and i + 1 < len(rest):
            status_filter = rest[i + 1]
            i += 2
        elif rest[i].startswith("--status="):
            status_filter = rest[i][len("--status="):]
            i += 1
        else:
            rest_no_set.append(rest[i])
            i += 1
    rest = rest_no_set
    project, rest2 = _pop_project(rest)
    if project and rest2:
        print("Error: --project and explicit task ID are mutually exclusive")
        return 1
    if project and not rest2:
        if downstream or enable_downstream:
            print("Usage: tq retry <project/task_id> --downstream [--enable-downstream]")
            return 1
        if set_args:
            print("Usage: tq retry <project/task_id> --set … (not with --project alone)")
            return 1
        return cmd_retry_project(home, project, status_filter=status_filter) or 0
    if rest2:
        if status_filter:
            print("Usage: --status is only valid with tq retry --project P")
            return 1
        if len(rest2) > 1:
            if downstream or enable_downstream or set_args:
                print("Usage: multi-ID retry cannot combine with --downstream / --set")
                return 1
            from taskqueue._cli_project import cmd_retry_keys

            return cmd_retry_keys(home, rest2)
        return cmd_retry(
            home,
            rest2[0],
            set_args=set_args or None,
            downstream=downstream,
            enable_downstream=enable_downstream,
        )
    print(
        "Usage: tq retry <project/task_id>… [--downstream] [--enable-downstream] "
        "[--set --flag=val ...] | tq retry --project P [--status STATUS]"
    )
    return 1


def _cmd_add(home: Path, rest: list[str]) -> int:
    project, rest2 = _pop_project(rest)
    if not rest2 or not project:
        print("Usage: tq add <yaml> --project PROJECT")
        print("       tq submit <yaml> --project PROJECT   (alias; goes through master HTTP)")
        return 1
    cmd_add(home, rest2[0], project)
    return 0


def _cmd_stop(home: Path, rest: list[str]) -> int:
    project, rest2 = _pop_project(rest)
    force, rest2 = _pop_flag(rest2, "--force")
    stop_reason, rest2 = _pop_value(rest2, "--reason")
    if project and rest2:
        print("Error: --project and explicit task ID are mutually exclusive")
        return 1
    if project and not rest2:
        return cmd_stop_project(home, project)
    if rest2:
        return cmd_stop(home, rest2[0], force=force, reason=stop_reason)
    print("Usage: tq stop <project/task_id> [--force] [--reason TEXT] | tq stop --project P")
    return 1


def _cmd_suspend(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq suspend <project/task_id>")
        return 1
    return cmd_suspend(home, rest[0])


def _cmd_pause(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq pause <project>")
        return 1
    if len(rest) > 1 or rest[0].startswith("-"):
        print("Usage: tq pause <project>")
        return 2
    return cmd_pause(home, rest[0]) or 0


def _cmd_resume(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq resume <project>")
        return 1
    if len(rest) > 1 or rest[0].startswith("-"):
        print("Usage: tq resume <project>")
        return 2
    return cmd_resume_project(home, rest[0]) or 0


def _cmd_freeze(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq freeze <project>")
        return 1
    if len(rest) > 1 or rest[0].startswith("-"):
        print("Usage: tq freeze <project>")
        return 2
    return cmd_freeze(home, rest[0]) or 0


def _cmd_unfreeze(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq unfreeze <project>")
        return 1
    if len(rest) > 1 or rest[0].startswith("-"):
        print("Usage: tq unfreeze <project>")
        return 2
    return cmd_unfreeze(home, rest[0]) or 0


def _cmd_patch(home: Path, rest: list[str]) -> int:
    if len(rest) < 2:
        print("Usage: tq patch <project/task_id> k=v [k=v ...]")
        return 1
    return cmd_patch(home, rest[0], rest[1:])


def _cmd_force(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq force <project/task_id>")
        return 1
    return cmd_force(home, rest[0])


def _cmd_reverify(home: Path, rest: list[str]) -> int:
    up_to = None
    key = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--up-to" and i + 1 < len(rest):
            up_to = rest[i + 1]
            i += 2
        elif a.startswith("--up-to="):
            up_to = a.split("=", 1)[1]
            i += 1
        elif not a.startswith("-") and key is None:
            key = a
            i += 1
        else:
            i += 1
    if not key or not up_to:
        print("Usage: tq reverify <project/root_task_id> --up-to <task_id>")
        return 1
    return cmd_reverify(home, key, up_to)


def _cmd_locks(home: Path, rest: list[str]) -> int:
    as_json, rest = _pop_flag(rest, "--json")
    return cmd_locks(home, as_json=as_json)


def _cmd_remove(home: Path, rest: list[str]) -> int:
    force, rest = _pop_flag(rest, "--force")
    project_flag, rest = _pop_value(rest, "--project")
    if project_flag and rest:
        print("Error: --project and explicit task ID are mutually exclusive")
        return 1
    if project_flag:
        return cmd_remove_project(home, project_flag, force=force)
    if not rest:
        print("Usage: tq remove <project/task_id> [--force]")
        print("       tq remove --project P [--force]   (purge all tasks+runs for project P from DB)")
        return 1
    return cmd_remove(home, rest[0], force=force)


def _cmd_priority(home: Path, rest: list[str]) -> int:
    force, rest = _pop_flag(rest, "--force")
    if len(rest) != 2:
        print("Usage: tq priority <project/task_id> <N> [--force]", file=sys.stderr)
        return 2
    try:
        n = int(rest[1])
    except ValueError:
        n = -1
    if not 0 <= n <= 100:
        print("Usage: tq priority <project/task_id> <N> [--force]  (N must be an integer 0-100)", file=sys.stderr)
        return 2
    return cmd_priority(home, rest[0], n, force=force)


def _cmd_migrate(home: Path, rest: list[str]) -> int:
    return cmd_migrate(home, rest)


def _cmd_config(home: Path, rest: list[str]) -> int:
    if not rest or rest[0] == "get":
        cmd_config_get(home)
        return 0
    if rest[0] == "set":
        return cmd_config_set(home, rest[1:])
    print("Usage: tq config [get | set k=v ...]")
    return 1


def _cmd_logs(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq logs <project/task_id> [--bytes N] [--grep PATTERN] [--jsonl] [-f]")
        return 1
    key = rest[0]
    max_bytes = None
    grep_pat = None
    use_jsonl = False
    follow = False
    i = 1
    while i < len(rest):
        if rest[i] == "--bytes" and i + 1 < len(rest):
            try:
                max_bytes = int(rest[i + 1])
            except ValueError:
                print("Usage: tq logs <project/task_id> [--bytes N]")
                return 1
            i += 2
        elif rest[i].startswith("--bytes="):
            try:
                max_bytes = int(rest[i].split("=", 1)[1])
            except ValueError:
                print("Usage: tq logs <project/task_id> [--bytes N]")
                return 1
            i += 1
        elif rest[i] == "--grep" and i + 1 < len(rest):
            grep_pat = rest[i + 1]
            i += 2
        elif rest[i].startswith("--grep="):
            grep_pat = rest[i].split("=", 1)[1]
            i += 1
        elif rest[i] == "--jsonl":
            use_jsonl = True
            i += 1
        elif rest[i] in ("-f", "--follow"):
            follow = True
            i += 1
        else:
            i += 1
    if follow:
        if grep_pat or use_jsonl:
            print("tq logs: -f cannot be combined with --grep / --jsonl", file=sys.stderr)
            return 2
        from taskqueue._cli_task import follow_log

        return follow_log(home, key, tail_bytes=max_bytes)
    if grep_pat or use_jsonl:
        return cmd_logs_extended(home, key, max_bytes=max_bytes, grep=grep_pat, jsonl=use_jsonl)
    return cmd_logs(home, key, max_bytes=max_bytes)


def _cmd_detect(home: Path, rest: list[str]) -> int:
    cmd_detect()
    return 0


def _cmd_doctor(home: Path, rest: list[str]) -> int:
    as_json, rest = _pop_flag(rest, "--json")
    check_torch, rest = _pop_flag(rest, "--torch")
    bad = _reject_args("doctor [--json] [--torch]", rest)
    if bad is not None:
        return bad
    from taskqueue.doctor_checks import run_checks

    result = run_checks(home)
    if as_json:
        import json as _json

        emit_json(result)
    else:
        cmd_doctor(home, check_torch=check_torch)
        print("checks:")
        for c in result["checks"]:
            print(f"  [{c['level']:>4}] {c['name']}: {c['detail']}")
    return 0 if result["ok"] else 1


def _cmd_log(home: Path, rest: list[str]) -> int:
    # Daemon stdout ({home}/daemon.log). Distinct from `tq logs` (per-task).
    if rest and rest[0] in ("-h", "--help"):
        print("Usage: tq log [--bytes N] [--grep PATTERN]")
        print("Tail {TASKQUEUE_HOME}/daemon.log (scheduler stdout).")
        print("For a task's output use: tq logs <project/task_id>")
        return 0
    vals, remaining = _parse_kv_flags(list(rest), {"--bytes": None, "--grep": None})
    if remaining and not all(a in ("-h", "--help") for a in remaining):
        print(f"Unknown argument: {remaining[0]}")
        print("Usage: tq log [--bytes N] [--grep PATTERN]")
        print("Hint: task logs are `tq logs <project/task_id>` (plural).")
        return 1
    max_bytes = None
    grep_pat = vals.get("--grep")
    if "--bytes" in vals:
        try:
            max_bytes = int(vals["--bytes"])
        except ValueError:
            print("Usage: tq log [--bytes N] [--grep PATTERN]")
            return 1
    return cmd_daemon_log(home, max_bytes=max_bytes, grep=grep_pat)


def _cmd_my_tasks(home: Path, rest: list[str]) -> int:
    project, rest2 = _pop_project(rest)
    operator = None
    i = 0
    while i < len(rest2):
        if rest2[i] == "--operator" and i + 1 < len(rest2):
            operator = rest2[i + 1]
            i += 2
        elif rest2[i].startswith("--operator="):
            operator = rest2[i].split("=", 1)[1]
            i += 1
        else:
            i += 1
    return cmd_my_tasks(home, operator, project)


def _cmd_capacity(home: Path, rest: list[str]) -> int:
    cmd_capacity(home)
    return 0


def _cmd_plan(home: Path, rest: list[str]) -> int:
    import json as _json

    from taskqueue.db import open_db
    from taskqueue.plan import build_plan

    as_json = "--json" in rest
    nums = [a for a in rest if a.isdigit()]
    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db, migrate=False)
    try:
        plan = build_plan(conn, limit=int(nums[0]) if nums else 20)
    finally:
        conn.close()
    if as_json:
        emit_json(plan)
        return 0
    print(f"running={plan['running']}  would_start={plan['would_start']}  (approximate: declared resources only)")
    for proj, b in sorted(plan.get("gpu_budget", {}).items()):
        print(f"  GPU budget {proj}: {b['used_hours']:.2f}/{b['limit_hours']:g} h today (UTC), {b['remaining_hours']:.2f} h left")
    for e in plan["tasks"]:
        mark = f"START #{e['start_order']}" if e["verdict"] == "start" else "blocked"
        print(f"  {mark:<9} {e['project']}/{e['task_id']}  p={e['priority']}  {e['reason']}")
    return 0


def _cmd_report(home: Path, rest: list[str]) -> int:
    project = None
    days = 7
    audit = False
    i = 0
    while i < len(rest):
        if rest[i] == "--project" and i + 1 < len(rest):
            project = rest[i + 1]
            i += 2
        elif rest[i] == "--days" and i + 1 < len(rest):
            days = int(rest[i + 1])
            i += 2
        elif rest[i] == "--audit-projects":
            audit = True
            i += 1
        else:
            i += 1
    if audit:
        return cmd_audit_projects(home)
    cmd_report(home, project, days)
    return 0


def _cmd_diagnose(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq diagnose <project/task_id>")
        return 1
    return cmd_diagnose(home, rest[0])


def _cmd_metrics(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq metrics <project/task_id> [--json] [--samples N]")
        return 1
    as_json = "--json" in rest
    samples = 60
    samples_raw, rest = _pop_value(list(rest), "--samples")
    if samples_raw is not None:
        try:
            samples = int(samples_raw)
        except ValueError:
            print("Usage: tq metrics <project/task_id> [--json] [--samples N]  (N must be an integer)")
            return 1
    return cmd_metrics(home, rest[0], as_json=as_json, samples=samples)


def _cmd_export(home: Path, rest: list[str]) -> int:
    with_logs = "--with-logs" in rest
    dest = next((a for a in rest if not a.startswith("--")), None)
    return cmd_export(home, dest, with_logs=with_logs)


def _cmd_import(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq import <path.tar.gz> [--force]")
        return 1
    force = "--force" in rest
    archive = next((a for a in rest if not a.startswith("--")), None)
    if not archive:
        print("Usage: tq import <path.tar.gz> [--force]")
        return 1
    return cmd_import(home, archive, force=force)


def _cmd_token(home: Path, rest: list[str]) -> int:
    if not rest or rest[0] != "generate":
        print("Usage: tq token generate")
        return 1
    cmd_token_generate(home)
    return 0


def _cmd_validate(home: Path, rest: list[str]) -> int:
    fix = False
    path: str | None = None
    for a in rest:
        if a == "--fix":
            fix = True
        elif a.startswith("-"):
            print("Usage: tq validate [--fix] [path.yaml]")
            return 1
        elif path is None:
            path = a
        else:
            print("Usage: tq validate [--fix] [path.yaml]")
            return 1
    return cmd_validate(home, path, fix=fix)


def _cmd_mcp(home: Path, rest: list[str]) -> int:
    return cmd_mcp(home)


def _cmd_resync(home: Path, rest: list[str]) -> int:
    from taskqueue._cli_project import cmd_resync

    if not rest or rest[0].startswith("--"):
        print("Usage: tq resync <project> [--worker <id>] [--all-workers]")
        return 1
    project = rest[0]
    worker_id = None
    all_workers = False
    i = 1
    while i < len(rest):
        if rest[i] == "--worker" and i + 1 < len(rest):
            worker_id = rest[i + 1]
            i += 2
        elif rest[i] == "--all-workers":
            all_workers = True
            i += 1
        else:
            i += 1
    return cmd_resync(home, project, worker_id=worker_id, all_workers=all_workers)


def _cmd_projects(home: Path, rest: list[str]) -> int:
    validate = bool(rest and rest[0] == "validate")
    return cmd_projects(home, validate=validate)


def _cmd_bundle(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq bundle <project>")
        return 1
    return cmd_bundle(home, rest[0])


def _worker_service_cmd(home: Path, rest: list[str]) -> int:
    """tq worker install-service|uninstall-service [--worker-id ID] [--home DIR] (id defaults from worker.yaml)."""
    verb = rest[0]
    worker_id_arg = None
    home_arg = home
    i = 1
    while i < len(rest):
        if rest[i] == "--worker-id" and i + 1 < len(rest):
            worker_id_arg = rest[i + 1]; i += 2
        elif rest[i] == "--home" and i + 1 < len(rest):
            home_arg = Path(rest[i + 1]); i += 2
        else:
            i += 1
    if not worker_id_arg:
        conf_p = home_arg / "worker.yaml"
        if conf_p.is_file():
            import yaml as _yaml
            try:
                _wconf = _yaml.safe_load(conf_p.read_text(encoding="utf-8")) or {}
                worker_id_arg = _wconf.get("worker_id")
            except Exception:
                pass
    if not worker_id_arg:
        print(f"Usage: tq worker {verb} --worker-id <id>", file=sys.stderr)
        return 2
    from taskqueue import worker as _worker

    fn = _worker.install_worker_service if verb == "install-service" else _worker.uninstall_worker_service
    return int(fn(home_arg, worker_id_arg) or 0)


def _cmd_worker(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq worker [list | remove <id> | setup --master <url> | --master <url> --worker-id <id>]")
        return 1
    if rest[0] == "list":
        return cmd_worker_list(home)
    if rest[0] == "sync":
        # tq worker sync [<worker_id>] [--master <url>] [--token T]
        # worker_id may appear before or after flags.
        master_url_arg = None
        auth_token_arg = os.environ.get("TQ_AUTH_TOKEN")
        worker_id_arg = None
        i = 1
        while i < len(rest):
            if rest[i] == "--master" and i + 1 < len(rest):
                master_url_arg = rest[i+1]; i += 2
            elif rest[i] == "--token" and i + 1 < len(rest):
                auth_token_arg = rest[i+1]; i += 2
            elif not rest[i].startswith("--"):
                worker_id_arg = rest[i]; i += 1
            else:
                i += 1
        if not worker_id_arg:
            print("Usage: tq worker sync <worker_id> [--master <url>] [--token T]")
            return 1
        return cmd_worker_sync(home, worker_id_arg, master_url=master_url_arg, auth_token=auth_token_arg)
    if rest[0] == "remove":
        if len(rest) < 2:
            print("Usage: tq worker remove <worker_id>")
            return 1
        return cmd_worker_remove(home, rest[1])
    if rest[0] in ("install-service", "uninstall-service"):
        return _worker_service_cmd(home, rest)
    if rest[0] == "build-wheel":
        return cmd_worker_build_wheel(home)
    if rest[0] == "rotate-credential":
        master_url_arg = None
        auth_token_arg = os.environ.get("TQ_AUTH_TOKEN")
        i = 1
        while i < len(rest):
            if rest[i] == "--master" and i + 1 < len(rest):
                master_url_arg = rest[i+1]; i += 2
            elif rest[i] == "--token" and i + 1 < len(rest):
                auth_token_arg = rest[i+1]; i += 2
            else:
                i += 1
        return cmd_worker_rotate_credential(home, master_url=master_url_arg, auth_token=auth_token_arg)
    if rest[0] == "set-trust":
        if len(rest) < 3:
            print("Usage: tq worker set-trust <worker_id> <tier>")
            return 1
        return cmd_worker_set_trust(home, rest[1], rest[2])
    if rest[0] == "config-master":
        from taskqueue._cli_worker import cmd_worker_config_master
        host = None
        out = None
        remote_only = False
        port = None
        i = 1
        while i < len(rest):
            if rest[i] == "--host" and i + 1 < len(rest):
                host = rest[i + 1]; i += 2
            elif rest[i] == "--out" and i + 1 < len(rest):
                out = Path(rest[i + 1]); i += 2
            elif rest[i] == "--port" and i + 1 < len(rest):
                port = int(rest[i + 1]); i += 2
            elif rest[i] == "--remote-only":
                remote_only = True; i += 1
            else:
                i += 1
        if not host:
            print("Usage: tq worker config-master --host <ip-or-hostname> [--out path] [--port N]")
            return 1
        return cmd_worker_config_master(
            home, host=host, out=out, remote_only=remote_only, port=port
        )
    if rest[0] == "setup":
        from taskqueue._cli_worker import cmd_worker_setup_from_join
        master_url = None
        worker_id = None
        auth_token = os.environ.get("TQ_AUTH_TOKEN")
        join_from = None
        i = 1
        while i < len(rest):
            if rest[i] == "--master" and i + 1 < len(rest):
                master_url = rest[i+1]
                i += 2
            elif rest[i] == "--worker-id" and i + 1 < len(rest):
                worker_id = rest[i+1]
                i += 2
            elif rest[i] == "--token" and i + 1 < len(rest):
                auth_token = rest[i+1]
                i += 2
            elif rest[i] == "--from" and i + 1 < len(rest):
                join_from = Path(rest[i + 1])
                i += 2
            else:
                i += 1
        if join_from is not None:
            return cmd_worker_setup_from_join(home, join_from, worker_id)
        if not master_url:
            print("Usage: tq worker setup --from <join.yaml> [--worker-id <id>]")
            print("   or: tq worker setup --master <url> [--worker-id <id>] [--token T]")
            return 1
        cmd_worker_setup(home, master_url, worker_id, auth_token=auth_token)
        return 0
    master_url = None
    worker_id = None
    auth_token = os.environ.get("TQ_AUTH_TOKEN")
    i = 0
    while i < len(rest):
        if rest[i] == "--master" and i + 1 < len(rest):
            master_url = rest[i+1]
            i += 2
        elif rest[i] == "--worker-id" and i + 1 < len(rest):
            worker_id = rest[i+1]
            i += 2
        elif rest[i] == "--token" and i + 1 < len(rest):
            auth_token = rest[i+1]
            i += 2
        else:
            i += 1

    if not master_url or not worker_id:
        conf_p = home / "worker.yaml"
        if conf_p.is_file():
            import yaml
            try:
                conf = yaml.safe_load(conf_p.read_text(encoding="utf-8")) or {}
                if not master_url:
                    master_url = conf.get("master_url")
                if not worker_id:
                    worker_id = conf.get("worker_id")
                if not auth_token:
                    auth_token = conf.get("auth_token")
            except Exception:
                pass

    if not master_url or not worker_id:
        print("Usage: tq worker --master <url> --worker-id <id> [--token T]")
        print("Or run `tq worker setup --master <url> [--worker-id <id>]` first to save configuration.")
        return 1
    cmd_worker_start(home, master_url, worker_id, auth_token=auth_token)
    return 0


def _cmd_group(home: Path, rest: list[str]) -> int:
    if len(rest) < 3:
        print("Usage: tq group <list|stop|retry> <project> <group_name>")
        return 1
    return cmd_group(home, rest[0], rest[1], rest[2])


def _cmd_suite(home: Path, rest: list[str]) -> int:
    if len(rest) < 2:
        print("Usage: tq suite <enable|disable|retry> <suite_name>")
        return 1
    return cmd_suite(home, rest[0], rest[1])


def _cmd_deps(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq deps <project[/task_id]> [--mermaid]")
        return 1
    use_mermaid, rest2 = _pop_flag(list(rest), "--mermaid")
    return cmd_deps(home, rest2[0] if rest2 else rest[0], mermaid=use_mermaid)


def _cmd_tail(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq tail <project/task_id> [--last N]")
        return 1
    last_n: int | None = None
    key = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--last" and i + 1 < len(rest):
            try:
                last_n = int(rest[i + 1])
            except ValueError:
                print("Usage: tq tail <project/task_id> [--last N]")
                return 1
            i += 2
        elif a.startswith("--last="):
            try:
                last_n = int(a.split("=", 1)[1])
            except ValueError:
                print("Usage: tq tail <project/task_id> [--last N]")
                return 1
            i += 1
        elif not a.startswith("-") and key is None:
            key = a
            i += 1
        else:
            i += 1
    if not key:
        print("Usage: tq tail <project/task_id> [--last N]")
        return 1
    if last_n is not None and last_n < 0:
        print("Error: --last N must be >= 0")
        return 1
    return cmd_tail(home, key, last_n=last_n)


def _cmd_result(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq result <project/task_id>")
        return 1
    return cmd_result(home, rest[0])


def _cmd_tag(home: Path, rest: list[str]) -> int:
    if len(rest) < 2:
        print("Usage: tq tag <add|remove|list> <project/task_id> [tag ...]")
        return 1
    tag_action = rest[0]
    tag_key = rest[1]
    tag_values = rest[2:]
    return cmd_tag(home, tag_action, tag_key, tag_values)


def _cmd_watch(home: Path, rest: list[str]) -> int:
    iv = 2.0
    iv_raw, rest = _pop_value(list(rest), "--interval")
    if iv_raw is not None:
        try:
            iv = float(iv_raw)
        except ValueError:
            print("Usage: tq watch [--interval N]  (N must be a number)")
            return 1
    if rest:
        print(f"Unknown argument: {rest[0]}")
        return 1
    return cmd_watch(home, interval=iv)


def _cmd_fair_share(home: Path, rest: list[str]) -> int:
    return cmd_fair_share(home)


def _cmd_sleep(home: Path, rest: list[str]) -> int:
    project, rest2 = _pop_project(rest)
    if not rest2 and not project:
        print("Usage: tq sleep <project/task_id> [--project P]")
        return 1
    key = rest2[0] if rest2 else ""
    return cmd_sleep(home, key, project)


def _cmd_wake(home: Path, rest: list[str]) -> int:
    project, rest2 = _pop_project(rest)
    if not rest2 and not project:
        print("Usage: tq wake <project/task_id> [--project P]")
        return 1
    key = rest2[0] if rest2 else ""
    return cmd_wake(home, key, project)


def _cmd_wait(home: Path, rest: list[str]) -> int:
    usage = (
        "Usage: tq wait [project/task_id ...] [--project P] [--suite S] [--group G] "
        "[--tag T]... [--mine | --started-by OP] [--downstream project/root] [--any] "
        "[--timeout SEC] [--poll SEC] [--json] [--raw-exit]"
    )
    keys: list[str] = []
    sel: dict = {}
    tags: list[str] = []
    timeout_val: float | None = None
    poll = 5.0
    mode = "all"
    as_json = raw_exit = False
    i = 0

    def _val(flag: str) -> str | None:
        nonlocal i
        a = rest[i]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
        if a == flag and i + 1 < len(rest):
            i += 1
            return rest[i]
        return None

    while i < len(rest):
        a = rest[i]
        name = a.split("=", 1)[0]
        if name in ("--timeout", "--poll"):
            v = _val(name)
            try:
                if name == "--timeout":
                    timeout_val = float(v)  # type: ignore[arg-type]
                else:
                    poll = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                print(usage)
                return 3
        elif name in ("--project", "--suite", "--group", "--started-by", "--downstream", "--tag"):
            v = _val(name)
            if v is None:
                print(usage)
                return 3
            if name == "--tag":
                tags.append(v)
            else:
                sel[{"--project": "project", "--suite": "suite", "--group": "group",
                     "--started-by": "started_by", "--downstream": "downstream_of"}[name]] = v
        elif a == "--mine":
            op = (os.environ.get("TQ_OPERATOR") or "").strip()
            if not op:
                print("--mine requires TQ_OPERATOR", file=sys.stderr)
                return 3
            sel["started_by"] = op
        elif a == "--any":
            mode = "any"
        elif a == "--json":
            as_json = True
        elif a == "--raw-exit":
            raw_exit = True
        elif a.startswith("--"):
            print(usage)
            return 3
        else:
            keys.append(a)
        i += 1
    if tags:
        sel["tags"] = tags
    if not keys and not sel:
        print(usage)
        return 3
    return cmd_wait(
        home, keys, timeout_sec=timeout_val, mode=mode, selectors=sel,
        as_json=as_json, raw_exit=raw_exit, poll_sec=poll,
    )

def _cmd_master_status(home: Path, rest: list[str]) -> int:
    return cmd_master_status(home)


def _cmd_master_migrate(home: Path, rest: list[str]) -> int:
    if not rest:
        print("Usage: tq master-migrate <target_url>  (e.g. http://192.168.1.x:8765)")
        return 1
    from taskqueue._cli_admin import cmd_master_migrate
    return cmd_master_migrate(home, rest[0])


def _wait_usage_project(rest: list[str]) -> str:
    """Project a ``tq wait`` targets: the single project of all keys/selectors, ``(multi)`` if several."""
    projects: set[str] = set()
    i = 0
    while i < len(rest):
        a = rest[i]
        name, eq, val = a.partition("=")
        if name in ("--project", "--downstream", "--suite", "--group", "--started-by", "--tag",
                    "--timeout", "--poll"):
            if not eq:
                i += 1
                val = rest[i] if i < len(rest) else ""
            if name == "--project" and val:
                projects.add(val)
            elif name == "--downstream" and "/" in val:
                projects.add(val.split("/", 1)[0])
        elif not a.startswith("--") and "/" in a:
            projects.add(a.split("/", 1)[0])
        i += 1
    if len(projects) > 1:
        return "(multi)"
    return next(iter(projects), "")


def _cli_usage_error_code(cmd: str, rest: list[str], rc: object) -> str:
    """``error_code`` for a CLI call. ``tq wait`` exit 1/2 are outcomes (``wait.*``), not errors."""
    if not rc:
        return ""
    if cmd == "wait":
        if "--raw-exit" in rest:
            return "wait.timeout" if rc == 124 else "wait.task_failed"
        if rc == 1:
            return "wait.task_failed"
        if rc == 2:
            return "wait.timeout"
    return f"exit.{rc}"


def _record_cli_call(home: Path, cmd: str, rest: list[str], t0: float, rc: object) -> None:
    """Count one top-level ``tq`` invocation in api_usage (name + timing only). Fail-open."""
    import time as _t

    if cmd in ("mcp", "health"):  # mcp counts its own tool calls; health is a status-line poll
        return
    try:
        from taskqueue import __version__
        from taskqueue.db import open_db
        from taskqueue.usage_track import record_direct

        db_path = _db(home)
        if not db_path.exists():
            return
        project = ""
        for j, a in enumerate(rest):
            if a == "--project" and j + 1 < len(rest):
                project = rest[j + 1]
            elif a.startswith("--project="):
                project = a.split("=", 1)[1]
        if cmd == "wait":
            project = _wait_usage_project(rest)
        conn = open_db(db_path, migrate=False)
        try:
            record_direct(
                conn, "cli", f"tq {cmd}", client=f"tq-cli/{__version__}",
                operator=os.environ.get("TQ_OPERATOR") or "", project=project,
                ms=(_t.monotonic() - t0) * 1000.0,
                error_code=_cli_usage_error_code(cmd, rest, rc),
            )
        finally:
            conn.close()
    except Exception:
        pass


def _cmd_agents(home: Path, rest: list[str]) -> int:
    """Usage analytics: interaction counters + task provenance outcomes."""
    import json as _json

    from taskqueue.agent_stats import agent_usage, format_table
    from taskqueue.db import open_db

    opts: dict = {"days": 7}
    as_json = False
    i = 0
    flags = {"--days": "days", "--by": "by", "--task-by": "task_by", "--project": "project",
             "--channel": "channel", "--operator": "operator", "--limit": "limit"}
    while i < len(rest):
        a = rest[i]
        name, eq, val = a.partition("=")
        if a == "--json":
            as_json = True
        elif name in flags:
            if not eq:
                i += 1
                val = rest[i] if i < len(rest) else ""
            opts[flags[name]] = int(val) if flags[name] in ("days", "limit") and val.isdigit() else val
        else:
            print("Usage: tq agents [--days N] [--by dims] [--task-by dims] [--project P] "
                  "[--channel C] [--operator O] [--limit N] [--json]")
            return 1
        i += 1
    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.", file=sys.stderr)
        return 1
    conn = open_db(db_path, migrate=False)
    try:
        data = agent_usage(conn, **opts)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    if as_json:
        emit_json(data)
        return 0
    print(format_table(data["usage"], f"Interactions, last {data['days']}d"))
    print()
    print(format_table(data["tasks"], f"Task submissions, last {data['days']}d"))
    if not data["tracking_enabled"]:
        print("\n(usage_tracking=0: interaction counters are not being recorded)")
    return 0


def _cmd_health(home: Path, rest: list[str]) -> int:
    """Red / amber / green verdict (taskqueue.health). Exit 0 green, 1 amber, 2 red.

    ``--deps`` prints the dependency / attention report instead (``_cli_admin.cmd_health``).
    """
    import json as _json

    from taskqueue.db import open_db
    from taskqueue.health import compute_health, format_line, read_ack, write_ack

    if "--deps" in rest:
        from taskqueue._cli_admin import cmd_health

        project, rest2 = _pop_project([a for a in rest if a != "--deps"])
        as_json, rest2 = _pop_flag(rest2, "--json")
        if rest2:
            print("Usage: tq health --deps [--project P] [--json]")
            return 3
        return cmd_health(home, project, as_json=as_json)
    unknown = [a for a in rest if a not in ("--line", "--plain", "--json", "--ack")]
    if unknown:
        print("Usage: tq health [--line] [--plain] [--json] [--ack] | tq health --deps [--project P] [--json]")
        return 3
    if "--ack" in rest:
        ack_at = write_ack(home, os.environ.get("TQ_OPERATOR"))
        print(f"Acknowledged failures up to {ack_at}.")
    db_path = _db(home)
    if not db_path.exists():
        print("No queue DB.", file=sys.stderr)
        return 3
    conn = open_db(db_path, migrate=False)
    try:
        health = compute_health(
            conn, ack_at=read_ack(home), daemon_up=_daemon_running(home) is not None
        )
    finally:
        conn.close()
    plain = "--plain" in rest
    if not plain:
        try:  # emoji on a cp1252 pipe (Windows status lines) would raise
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            plain = True
    if "--json" in rest:
        emit_json(health, ensure_ascii=plain)
    elif "--line" in rest:
        print(format_line(health, icons=not plain, color=plain and sys.stdout.isatty()))
        return 0  # status-line hosts may hide the output of a failing command
    else:
        print(format_line(health, icons=not plain, color=plain and sys.stdout.isatty()))
        for title, kind in (("New failures", "new_failed"), ("Frozen", "frozen"), ("Interrupted", "interrupted")):
            if health[kind]:
                more = health["counts"][kind] - len(health[kind])
                print(f"  {title}: {', '.join(health[kind])}" + (f" (+{more} more)" if more > 0 else ""))
        if health["counts"]["new_failed"]:
            print("  Triage with `tq status` / `tq diagnose <key>`; `tq health --ack` turns red into amber.")
    return {"green": 0, "amber": 1, "red": 2}[health["level"]]


def _cmd_usage(home: Path, rest: list[str]) -> int:
    import json as _json
    import time as _time
    path = home / "usage.jsonl"
    if not path.exists():
        print("No usage recorded yet.")
        return 0
    counts: dict[str, int] = {}
    last_seen: dict[str, int] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = _json.loads(line)
                c = e.get("cmd", "?")
                counts[c] = counts.get(c, 0) + 1
                ts = e.get("ts", 0)
                if ts > last_seen.get(c, 0):
                    last_seen[c] = ts
            except Exception:
                pass
    print(f"{'command':<20} {'uses':>6}  last seen")
    print("-" * 46)
    for c, n in sorted(counts.items(), key=lambda x: -x[1]):
        ago = int(_time.time()) - last_seen.get(c, 0)
        if ago < 3600:
            when = f"{ago//60}m ago"
        elif ago < 86400:
            when = f"{ago//3600}h ago"
        else:
            when = f"{ago//86400}d ago"
        print(f"  {c:<18} {n:>6}  {when}")
    return 0


def _cmd_prune(home: Path, rest: list[str]) -> int:
    from taskqueue._cli_prune import cmd_prune
    apply = "--apply" in rest
    verbose = "--verbose" in rest or "-v" in rest
    return cmd_prune(home, dry_run=not apply, verbose=verbose)


def _cmd_clone(home: Path, rest: list[str]) -> int:
    """tq clone <project/task_id> <new_task_id> [--set key=value ...] — new task from an existing one."""
    sets: list[str] = []
    while True:
        val, rest = _pop_value(rest, "--set")
        if val is None:
            break
        sets.append(val)
    if len(rest) != 2 or "/" not in rest[0]:
        print("Usage: tq clone <project/task_id> <new_task_id> [--set key=value ...]", file=sys.stderr)
        return 2
    from taskqueue.clone import clone_task_yaml, parse_set_args
    from taskqueue.config import db_path, load_projects
    from taskqueue.db import open_db

    project, task_id = rest[0].split("/", 1)
    if not db_path(home).is_file():
        print("No queue database yet.", file=sys.stderr)
        return 1
    conn = open_db(db_path(home), migrate=False)
    try:
        dest = clone_task_yaml(conn, load_projects(home), project, task_id, rest[1], parse_set_args(sets))
    except (ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Wrote {dest}. It is queued on the next sync (tq sync, or within one scheduler tick).")
    return 0


def _cmd_top(home: Path, rest: list[str]) -> int:
    """tq top [--once] [--json] — live view of running tasks pushed by the daemon (Ctrl+C to exit)."""
    once, rest = _pop_flag(rest, "--once")
    as_json, rest = _pop_flag(rest, "--json")
    bad = _reject_args("top [--once] [--json]", rest)
    if bad is not None:
        return bad
    from taskqueue.top import run_top

    return run_top(home, once=once, as_json=as_json)


def _cmd_diff(home: Path, rest: list[str]) -> int:
    """tq diff <run_a> <run_b> [--json] — compare two runs field by field."""
    as_json, rest = _pop_flag(rest, "--json")
    if len(rest) != 2 or not all(a.isdigit() for a in rest):
        print("Usage: tq diff <run_a> <run_b> [--json]  (run ids, see `tq status`)", file=sys.stderr)
        return 2
    from taskqueue.db import open_db
    from taskqueue.run_diff import diff_runs, render_diff

    dbp = _db(home)
    if not dbp.exists():
        print(f"No queue DB at {dbp}", file=sys.stderr)
        return 3
    try:
        d = diff_runs(open_db(dbp), int(rest[0]), int(rest[1]))
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if as_json:
        emit_json(d)
    else:
        print(render_diff(d))
    return 0


def _cmd_why(home: Path, rest: list[str]) -> int:
    """tq why <project/task_id> [--json] — why a task is (not) running."""
    as_json, rest = _pop_flag(rest, "--json")
    if len(rest) != 1 or "/" not in rest[0]:
        print("Usage: tq why <project/task_id> [--json]", file=sys.stderr)
        return 2
    from taskqueue.config import db_path
    from taskqueue.db import open_db
    from taskqueue.why import explain_task, format_explanation

    if not db_path(home).is_file():
        print("No queue database yet.", file=sys.stderr)
        return 1
    project, task_id = rest[0].split("/", 1)
    conn = open_db(db_path(home), migrate=False)
    try:
        info = explain_task(conn, project, task_id)
    finally:
        conn.close()
    if as_json:
        import json as _json

        emit_json(info)
    else:
        print(format_explanation(info))
    return 0 if info.get("ok") else 1


def _cmd_completion(home: Path, rest: list[str]) -> int:
    """tq completion bash|zsh|pwsh — print a completion script built from the command table."""
    from taskqueue.cli_table import build_table, completion_script

    if len(rest) != 1 or rest[0] not in ("bash", "zsh", "pwsh"):
        print("Usage: tq completion bash|zsh|pwsh", file=sys.stderr)
        return 2
    print(completion_script(rest[0], build_table(__doc__ or "", COMMANDS)), end="")
    return 0


COMMANDS: dict[str, Callable[[Path, list[str]], int]] = {
    "completion": _cmd_completion,
    "why": _cmd_why,
    "diff": _cmd_diff,
    "top": _cmd_top,
    "clone": _cmd_clone,
    "start": _cmd_start,
    "stop-daemon": _cmd_stop_daemon,
    "restart": _cmd_restart,
    "status": _cmd_status,
    "list": _cmd_list,
    "describe": _cmd_describe,
    "sync": _cmd_sync,
    "archive": _cmd_archive,
    "retry": _cmd_retry,
    "add": _cmd_add,
    "submit": _cmd_add,
    "stop": _cmd_stop,
    "suspend": _cmd_suspend,
    "pause": _cmd_pause,
    "resume": _cmd_resume,
    "freeze": _cmd_freeze,
    "unfreeze": _cmd_unfreeze,
    "patch": _cmd_patch,
    "force": _cmd_force,
    "reverify": _cmd_reverify,
    "locks": _cmd_locks,
    "remove": _cmd_remove,
    "priority": _cmd_priority,
    "migrate": _cmd_migrate,
    "config": _cmd_config,
    "logs": _cmd_logs,
    "detect": _cmd_detect,
    "doctor": _cmd_doctor,
    "log": _cmd_log,
    "my-tasks": _cmd_my_tasks,
    "capacity": _cmd_capacity,
    "plan": _cmd_plan,
    "report": _cmd_report,
    "diagnose": _cmd_diagnose,
    "metrics": _cmd_metrics,
    "export": _cmd_export,
    "import": _cmd_import,
    "token": _cmd_token,
    "validate": _cmd_validate,
    "mcp": _cmd_mcp,
    "projects": _cmd_projects,
    "resync": _cmd_resync,
    "bundle": _cmd_bundle,
    "worker": _cmd_worker,
    "group": _cmd_group,
    "suite": _cmd_suite,
    "deps": _cmd_deps,
    "tail": _cmd_tail,
    "result": _cmd_result,
    "tag": _cmd_tag,
    "watch": _cmd_watch,
    "fair-share": _cmd_fair_share,
    "sleep": _cmd_sleep,
    "wake": _cmd_wake,
    "wait": _cmd_wait,
    "master-status": _cmd_master_status,
    "master-migrate": _cmd_master_migrate,
    "usage": _cmd_usage,
    "agents": _cmd_agents,
    "health": _cmd_health,
    "prune": _cmd_prune,
}


_KNOWN_FLAGS: set[str] | None = None


def _known_flags() -> set[str]:
    """Every long option the CLI understands: flags in the help text plus flag literals in the package."""
    global _KNOWN_FLAGS
    if _KNOWN_FLAGS is None:
        import re

        flags = set(re.findall(r"(--[a-z0-9][a-z0-9-]*)", __doc__ or ""))
        for p in Path(__file__).parent.glob("*.py"):
            try:
                flags.update(re.findall(r"[\"'](--[a-z0-9][a-z0-9-]*)", p.read_text(encoding="utf-8")))
            except OSError:
                pass
        _KNOWN_FLAGS = flags
    return _KNOWN_FLAGS


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    home = _home()

    # I-01: Force UTF-8 stdout on Windows to avoid cp1252 crashes on box-drawing chars
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    if not args or args[0] in ("--help", "-h", "help"):
        print(__doc__)
        return 0

    cmd, *rest = args

    # Record command usage (fire-and-forget; never raises)
    def _record_usage(command: str, flags: list[str]) -> None:
        import json
        import time
        try:
            path = home / "usage.jsonl"
            entry = {"ts": round(time.time()), "cmd": command,
                     "flags": [f for f in flags if f.startswith("-")]}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    if cmd not in ("usage", "agents", "health", "status", "list", "watch", "doctor", "workers", "logs", "log", "tail", "result"):
        _record_usage(cmd, rest)

    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd}\n{__doc__}", file=sys.stderr)
        return 2
    opts = rest[: rest.index("--")] if "--" in rest else rest
    if "--help" in opts or "-h" in opts:
        # Never run the command for a help request; show its usage lines.
        from taskqueue.cli_table import build_table

        lines = build_table(__doc__ or "", COMMANDS)[cmd].usage
        print("\n".join(lines) if lines else __doc__)
        return 0
    unknown = [a for a in opts if a.startswith("--") and a.split("=", 1)[0] not in _known_flags()]
    if unknown:
        print(f"Unknown option(s) for tq {cmd}: {' '.join(unknown)}   (see: tq {cmd} --help)", file=sys.stderr)
        return 2
    import time as _t

    _t0 = _t.monotonic()
    rc = handler(home, rest)
    _record_cli_call(home, cmd, rest, _t0, rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
