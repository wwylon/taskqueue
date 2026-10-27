# taskqueue

Local resource-aware task queue: a single daemon plus `tq` CLI, web UI, and MCP server that schedule YAML tasks across one or more project folders (CPU / RAM / GPU VRAM admission, checkpoints, remote workers).

Web UI: `http://127.0.0.1:8765` (live SSE via `/api/events`). Terminal: `tq watch`. Tour: **[docs/operators/web-ui.md](docs/operators/web-ui.md)**.

**UI:** Queue, Dashboard, History, Workers, Resources, Usage, Groups, Deps, Tasks, Timeline, Schedule, Alerts, Audit, API; themes Dark/Auto/Light; locales **EN/PL/ZH/AR**.

## Documentation

* [Documentation hub](docs/README.md) — help topics, operators, agents (single source for UI **?** and **Docs** tab)
* [Web UI](docs/operators/web-ui.md) — sidebar pages, queue tools, alerts, audit, scoped API tokens
* [Security](docs/security.md) — loopback bind, `auth_token`, scoped tokens, operator audit
* [Task Standards](docs/task_standards.md) — progress contract, checkpoint/`resume_args`, resources, YAML field reference, dispatch/workers
* [Scheduler](docs/scheduler.md) — admission estimates, fast handoff on local exit, additive RAM gate, staggering, and victim selection under pressure
* [MCP server](docs/mcp.md) — configure Cursor/Claude for `tq mcp`; tool reference
* [examples/](examples/) — minimal sample project; dogfood fixtures in `fixtures/tqdemo/`
* [Error codes](docs/errors.md) — stable `tq.*` codes (`last_error_code` / attention)
* [Task clients](clients/README.md) — cooperative suspend helpers (`tq_client`)
* [Master–worker configuration](docs/WORKER_SETUP.md) — step-by-step setup + full bind/auth/`worker.yaml` reference
* [Master–worker architecture](docs/master_worker.md) — dispatch, sync, logs, disconnect, multi-worker
* [Fixtures `tqdemo`](fixtures/tqdemo/README.md) — cheap dummy tasks for dogfood/CI; stress and test tasks
* [Changelog](CHANGELOG.md) — released versions
* License: MIT

## Installation

**Prerequisites:** Python **3.12+**, [Git](https://git-scm.com/), and [`uv`](https://docs.astral.sh/uv/).

### 1. Install `uv`

```powershell
python -m pip install uv
```

Or via winget: `winget install astral-sh.uv`. Confirm with `uv --version`.

### 2. Clone and sync

```powershell
git clone https://github.com/wwylon/taskqueue.git
cd taskqueue
uv sync
```

This creates `.venv`, installs runtime deps (`psutil`, `pyyaml`, `pynvml`), the `tq` / `taskqueue` console scripts, and the optional `dev` group (`pytest`).

### Optional: Windows toast popups (`winotify`)

System alerts and task-end toasts on Windows need the optional [`winotify`](https://pypi.org/project/winotify/) package. Without it, alerts still fire (Alerts page fire counts, notification bell, in-page UI toasts) but OS Action Center popups are skipped; `tq doctor` warns when Windows popup is enabled and `winotify` is missing.

```powershell
# project venv
uv sync --extra toast
# or
uv pip install "winotify>=1.1.0"

# if tq is installed into a system Python (e.g. Scripts\tq.exe)
python -m pip install "winotify>=1.1.0"
# or: python -m pip install "taskqueue[toast]"
```

Then `tq restart` so the daemon picks up the package. See [Alerts](docs/help/page-alerts.md) and config key `toast_notifications` in [WORKER_SETUP](docs/WORKER_SETUP.md).

### 3. Verify

```powershell
uv run tq doctor
# or, with the venv activated:
.\.venv\Scripts\Activate.ps1
tq doctor
```

You should see home path, schema version, and `daemon: not running` before first start.

### Alternative: pip editable install

If you prefer system/user site-packages instead of a project `.venv`:

```powershell
python -m pip install -e .
# optional tests: python -m pip install pytest
# optional Windows toasts: python -m pip install -e ".[toast]"
tq doctor
```

(`dependency-groups.dev` is a `uv`/`PEP 735` group, not a pip extra — use `uv sync` for pytest, or install `pytest` separately. The `toast` extra is a normal pip optional-dependency for `winotify`.)

### Optional: Windows autostart

After `tq` is on `PATH`, register a logon task so the daemon comes back after reboot:

```powershell
powershell -File scripts\install_tq_autostart.ps1 -AlsoOnUnlock
```

## Quickstart

After installation:

```powershell
cd path\to\taskqueue
uv sync   # if not already done
```

Register projects in `~/.taskqueue/projects.yaml` (created on first use if missing):

```yaml
projects:
  myapp:
    root: C:\Users\you\projects\myapp   # or ~/projects/myapp
    # queue_dir: experiments/operator_queue   # default
```

To **remove** a project:
1. Delete its entry from `~/.taskqueue/projects.yaml`.
2. Purge its tasks and runs from the DB: `tq remove --project <name>` (add `--force` if tasks are running).
3. On Windows, restart the daemon first (`tq stop-daemon && tq start-daemon`) to release any file handles before deleting the project directory.

```powershell
uv run tq start
uv run tq list
```

Drop an enabled YAML under `<project>/experiments/operator_queue/`; the daemon syncs every ~60s (or run `tq sync`).

**Try the bundled sample:** register `examples/minimal` as a project (see [`examples/minimal/README.md`](examples/minimal/README.md)), then:

```powershell
uv run tq sync
uv run tq patch minimal/hello-progress enabled=true
uv run tq wait minimal/hello-progress --timeout 60
```

(The sample YAML ships `enabled: false` so it does not run until you patch or edit it.)

## Publish / security (Slice H)

* Default bind is **`127.0.0.1`** (`tq config set bind_host=0.0.0.0` only when intentional).
* Optional Bearer auth: `tq token generate` (or `tq config set auth_token=…`). Clients send `Authorization: Bearer …` or `X-TQ-Token`. UI on a non-loopback host: `localStorage.setItem('tq_token', '…')`. Workers: `--token` / `TQ_AUTH_TOKEN`. Loopback clients skip the token check. Scoped `tq_live_…` tokens (web **API** page) are a separate credential; workers keep using `auth_token`. See [docs/security.md](docs/security.md).
* Master move: `tq export` / `tq import --force` (stop daemon first). Bundles use safe tar extract.
* Windows: `prevent_sleep=1` (default) inhibits sleep while local tasks run (complements clock-jump reclaim).

## State home

Default `~/.taskqueue/` (override with `TASKQUEUE_HOME`):

```
~/.taskqueue/
  projects.yaml
  queue.db
  daemon.pid / daemon.log
  logs/<project>/
  bundles/<project>/     # optional binary bundles for workers
```

### Daemon home

Default state lives under `~/.taskqueue` (port **8765**). Confirm with `tq doctor`.

**Demo fixtures:** register `fixtures/tqdemo` as project `tqdemo` in `~/.taskqueue/projects.yaml` (see [`fixtures/tqdemo/README.md`](fixtures/tqdemo/README.md)). All dummy YAMLs ship `enabled: false`.

## Testing

### Unit and integration tests

```powershell
cd path\to\taskqueue
uv run pytest tests/ -q
```

297 tests. Takes ~3–4 minutes (mostly I/O bound). Run after any code change.

```powershell
# Run a specific file fast:
uv run pytest tests/test_web_routes.py -q
```

### Live UI testing with tqdemo

Register the `tqdemo` fixture project and run the tasks listed in [`fixtures/tqdemo/README.md`](fixtures/tqdemo/README.md):

```powershell
cd path\to\taskqueue
uv run tq start

# Enable a task, sync, then watch it run:
uv run tq patch tqdemo/dummy-progress enabled=true
uv run tq sync --project tqdemo
# open http://127.0.0.1:8765
```
**Scheduler coverage** — one task per scenario:

| Task | Tests |
|------|-------|
| `dummy-progress` | Happy-path `(N%)` progress bar |
| `dummy-resume` | Checkpoint / `resume_args` round-trip |
| `dummy-deps-a` → `dummy-deps-b` | `depends_on` chain |
| `dummy-exclusive` | Exclusive gating |
| `dummy-exclusive-project` | Per-project exclusive gating |
| `dummy-frozen` | Freeze detection (daemon marks error after threshold) |
| `dummy-lie-progress` | Progress-suspect detection |
| `dummy-ask-cpu` / `dummy-ask-gpu` | Scheduler rejection without machine stress |
| `dummy-fail` | Non-zero exit / retry flow |
| `dummy-cap` | `wall_clock_cap_sec` enforcement |
| `dummy-suspend` | Cooperative suspend via exit 75 |

**Test suite tasks** (run actual pytest inside the queue):

```powershell
uv run tq patch tqdemo/test-stress enabled=true && uv run tq sync --project tqdemo
# watch progress bar fill as each test passes (~100 s)

uv run tq patch tqdemo/test-scale enabled=true && uv run tq sync --project tqdemo
# scale/latency tests, ~1 s
```

**Live UI stress workflow** (web interface load testing):

```powershell
# 1. Fill the DB with 1 000 fake tasks across 40 projects
uv run tq patch tqdemo/stress-generate enabled=true && uv run tq sync --project tqdemo
# Open http://127.0.0.1:8765, filter by project, check project dropdown

# 2. Stream a 120 MB log to stress the log viewer
uv run tq patch tqdemo/stress-biglog enabled=true && uv run tq sync --project tqdemo
# Open the log viewer mid-run; verify no browser freeze

# 3. Clean up when done
uv run tq patch tqdemo/stress-cleanup enabled=true && uv run tq sync --project tqdemo
```

## CLI

Exit codes: `0` ok, `1` error, `2` usage (unknown flags or extra args are rejected, nothing is executed), `3` master unreachable.

| Command | Purpose |
|---------|---------|
| `tq start` | Start daemon (background, detached) |
| `tq completion bash\|zsh\|pwsh` | Print a shell completion script (commands, subcommands, flags), e.g. `tq completion bash >> ~/.bashrc` or `tq completion pwsh >> $PROFILE`. `tq <command> --help` prints that command's usage without running it |
| `tq stop-daemon [--force]` | Graceful shutdown; refuses while tasks are running (they would be stopped and re-queued from scratch) unless `--force` |
| `tq restart [--timeout=SEC] [--force]` | Stop, wait for exit, then start (default wait 180s); refuses while tasks are running unless `--force` |
| `tq status [--project P]` | Running + recent runs. Each running task shows `last progress N ago` and its ETA from the recent progress rate; `POSSIBLY STALLED` when the last `%` change is older than 3x the run's usual interval (and at least 2 min). `--json` adds `progress_health` |
| `tq clone <project/task_id> <new_id> [--set key=value ...]` | Use a task as a template: copies its YAML document to `<queue_dir>/<new_id>.yaml` with the new id and any `--set` overrides (values parsed as YAML, e.g. `--set priority=80 --set 'args=[--lr, "0.1"]'`). Validated before writing; refuses matrix sources and existing ids. History's **Re-run** re-queues a task with one run's recorded args instead |
| `tq diff <run_a> <run_b> [--json]` | Compare two runs field by field: status, exit code, duration, peak RAM/VRAM/CPU, worker, with numeric deltas. Exit 3 if a run id is unknown |
| `tq why <project/task_id> [--json]` | Why a task is (not) running: the scheduler's last block reason, unmet dependency, paused project, `max_attempts`, and a dry-run verdict for queued tasks |
| `tq list [--project P] [--tag T] …` | Full queue table; multiple `--tag` flags filter by AND. `--status failed,queued,disabled` (comma-separated or repeatable). Bracketed codes distinguish outcomes (e.g. `[tq.run.cap]` vs `[tq.run.nonzero]`); grinding cap auto-retries show `cap×N/M`. `--json` includes `enabled`, `progress_pct`, `requires_gpu`, `cap_auto_retry_count` |
| `tq health [--line] [--plain] [--json] [--ack]` | Red / amber / green queue verdict shared with the web header and status lines: **red** = a task failed (or ended `unknown`) since the last acknowledge, or the daemon is down; **amber** = acknowledged failures, cap-`interrupted` tasks or frozen runs; **green** = nothing needs a human. Exit **0/1/2** = green/amber/red (`--line` always 0). `--line` is one emoji line for status bars (`--plain` = ASCII), `--ack` turns current failures from red to amber |
| `tq health --deps [--project P] [--json]` | Submitted-task health: status counts, broken/missing `depends_on`, attention, disabled. Exit **1** if missing deps or attention items |
| `tq attention [--project P] [--json]` | Tasks needing attention (same set as MCP `attention_list`) |
| `tq archive [PROJECT ...] [--older-than 30d] [--status completed,failed] [--dry-run]` | Take old finished tasks out of the live queue (default: `completed`, older than 30 days). Each task and its runs are appended to `<home>/archive/<project>.jsonl`, its YAML is moved to `<queue_dir>/archive/` (not synced), and the DB rows are deleted. Running and queued tasks are never archived; tasks that share a multi-task YAML are skipped. Use `--dry-run` first |
| `tq sync [PROJECT ...] [--force] [--require-daemon]` | Re-sync YAML dirs via daemon (HTTP wait up to 10 min). Only changed YAML files are re-read (mtime cache shared with the daemon tick), so a warm sync is fast even with thousands of tasks; `--force` re-reads everything (use it to revert DB-only edits). `PROJECT` limits the sync to named projects. True offline repair exits **2** and prints `[OFFLINE]`; if the daemon pid is up but sync times out, exits **2** without SQLite writes. The same rule applies to `tq stop`, `tq stop-project`, `tq patch` and other mutations: when the daemon pid is alive but does not answer, the CLI reports an error instead of writing SQLite directly. `--require-daemon` refuses offline writes |
| `tq add <yaml> --project P` | Enqueue from a YAML file |
| `tq validate [--fix] [path]` | Lint registered (or one) `operator_queue` YAML. `--fix` rewrites known legacy keys (`ram_gib`→`resources.ram_gib`, `timeout_s`→`wall_clock_cap_sec`, `hypothesis`→`x-hypothesis`, drop matching `project`, `name`→`description`); may drop comments |
| `tq stop <project/task_id> [--force]` | Graceful stop (checkpoint first). Blocked with an error if `started_by` differs from your `TQ_OPERATOR` — pass `--force` to override |
| `tq stop --project P` | Stop all running tasks in project P |
| `tq retry <project/task_id>… [--downstream] [--enable-downstream]` | Re-queue; `--downstream` retries transitive dependents and prints requeued/skipped; `--enable-downstream` enables then retries. Multiple IDs batch in one DB transaction. |
| `tq retry --project P [--status failed[,interrupted,…]]` | Retry terminal-retryable in P; optional `--status` filter (comma-separated). Prefers daemon `/api/tasks/retry-batch`. |
| `tq result <project/task_id>` | Print last non-empty log line; exit 0 iff task `completed` (agents: prefer over `tq tail`) |
| `tq agents [--days N] [--by dims] [--task-by dims] [--json]` | Usage analytics: who calls what (channel/client/operator/project/op) and task provenance + outcomes (`/api/agent-usage`, MCP `agent_usage_summary`) |
| `tq wait <project/task_id ...> [--suite S\|--group G\|--tag T\|--mine] [--any] [--timeout N] [--json]` | Block until the selected tasks settle; exit 0 all completed, 1 failed/stopped, 2 timeout, 3 not found (`--raw-exit`: legacy single-task codes). MCP: `wait_for_task` |
| `tq tail <project/task_id> [--last N]` | Follow log, or dump last N lines and exit (no hang on completed) |
| `tq pause <project>` / `tq resume <project>` | Pause / unpause scheduling for a project (soft) |
| `tq freeze <project>` / `tq unfreeze <project>` | **Block new starts** for one project only (`tq.block.frozen`); running continue |
| `tq patch <project/task_id> k=v …` | Patch YAML + sync (`priority`, `enabled`, `wall_clock_cap_sec`, `max_attempts`). `enabled=true` on a terminal task **warns** — still needs `tq retry`. Prints `field: old -> new` and exits 1 if a field did not change in the queue database; exits 3 if a daemon runs but does not answer |
| `tq force <project/task_id>` | One-shot bypass of soft resource / anti-flap gates (re-queues; UI shows a toast) |
| `tq group list\|stop\|retry <project> <group>` | List / stop running / retry failed tasks in a YAML `group:` |
| `tq remove <project/task_id> [--force]` | Remove one task from DB. Same cross-operator `--force` gate as `tq stop` |
| `tq remove --project P [--force]` | Purge **all** tasks + runs for project P from DB (remove its `projects.yaml` entry too) |
| `tq priority <project/task_id> <N> [--force]` | Change priority live (higher N = sooner). Same cross-operator `--force` gate as `tq stop` |
| `tq migrate priority-invert [--dry-run]` | One-shot remap `priority → 100−N` when switching to higher=sooner; sets `priority_higher_wins` |
| `tq capacity` | Master + workers free CPU/RAM/GPU (per-device `gpus[]`) |
| `tq plan [N]` | Dry-run: which queued tasks would start next, and why others are blocked (approximate) |
| `tq report [--project P] [--days N]` | Aggregate runs / error codes |
| `tq diagnose <project/task_id>` | Deep bundle (runs, log tail, worker, metrics) |
| `tq metrics <project/task_id> [--json] [--samples N]` | Same run metrics as the UI (CPU/RAM/VRAM/GPU avg, `requires_gpu`, log warn/err counts) |
| `tq mcp` | Stdio MCP for agents — **setup** (Cursor/Claude) + tools in [`docs/mcp.md`](docs/mcp.md); opt-in YAML `notify_url` in [`docs/task_standards.md`](docs/task_standards.md) |
| `tq export [path] [--with-logs]` | Export master home (DB + projects.yaml) |
| `tq import <path> [--force]` | Restore master home from export |
| `tq token generate` | Create and store `auth_token` |
| `tq config get` / `tq config set k=v …` | Resource / daemon config (`bind_host`, `auth_token`, `prevent_sleep`, `placement_mode=binpack\|first_fit`) |
| `tq watch [--interval N]` | Live terminal dashboard (rich if available; ANSI fallback) |
| `tq top [--once] [--json]` | Live view of running tasks (progress, elapsed, RAM, CPU, last log line) redrawn when the daemon pushes an update over `/api/events`; `--once` prints one frame; `--json` emits one compact JSON object per line (NDJSON) instead of redrawing. Exit 3 if the daemon is unreachable |
| `tq suite enable\|disable\|retry <suite>` | Act on all tasks sharing a `suite:` label at once |
| `tq deps <project/task_id> [--mermaid]` | Show dependency tree; `--mermaid` emits Mermaid diagram |
| `tq tag add\|remove\|list <project/task_id> [tags…]` | Manage task tags live |
| `tq logs <project/task_id> [--grep PAT] [--jsonl] [-f]` | Tail latest **task** log; `--grep` filters lines, `--jsonl` parses structured output, `-f` follows new output until the task settles (exit 0 completed, 1 failed, 3 not found — same as `tq wait`) |
| `tq log [--bytes N] [--grep PAT]` | Tail **daemon** stdout (`{home}/daemon.log`) — soak / SHADOW / admission lines |
| `tq detect` | Detect hardware |
| `tq doctor [--json] [--torch]` | Home, uptime, DB, projects, memory, port; daemon line is `running` / `UNREACHABLE` / `not running` (HTTP probe). Then checks, each `ok` / `warn` / `fail`: daemon HTTP, stale pid file, `PRAGMA quick_check` (read-only), WAL size, free disk, project roots and queue dirs. Exits 1 when a check fails; `--json` prints only the checks. `--torch` also checks `torch.cuda` (importing torch takes tens of seconds, so it is off by default); the RAM-consumer scan stops after 5 s and says so |
| `tq reverify <project/root> --up-to <task_id>` | Enable root→target, disable other downstream, retry with skip report |
| `tq locks [--json]` | Active `resources.locks` holders and waiters |
| `tq projects` | List registered projects |
| `tq bundle <project>` | Pack project binary bundle for workers |
| `tq worker list` | List workers |
| `tq worker remove <worker_id>` | Deregister worker |
| `tq worker setup --master <url>` | Clone projects / init venvs on worker |
| `tq worker --master <url> --worker-id <id>` | Start worker client |

## Session work vs the queue

Taskqueue only schedules YAML under a project's `experiments/operator_queue/`. It does **not** parse planning docs. Keep interactive/session work out of the queue; put long jobs (>~2 min) in YAML and `tq sync`.
