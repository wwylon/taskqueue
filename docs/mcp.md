# MCP server (`tq mcp`)

`tq mcp` runs a minimal stdio JSON-RPC 2.0 MCP server (no extra dependency —
implemented directly in `tqdaemon/mcp_server.py`). It exposes observation
tools and guarded task mutations so an agent can submit, wait, diagnose, and
retry work without scraping the web UI or shelling out to `tq`.

It talks to the **same home** the CLI would use (`TQDAEMON_HOME` env var,
default `~/.tqdaemon`). **Mutations** (`enqueue_task` / `retry_task` / `patch_task` /
`stop_task`) go through the daemon HTTP API when it is reachable (same as `tq`);
reads still use SQLite. Set `TQ_MASTER` to aim at a remote master.

## Setup

### Prerequisites

- `tq` on `PATH` (from an editable install / `uv sync` of the tqdaemon checkout).
- The daemon does **not** need to be running for most read tools (they open
  `{TQDAEMON_HOME}/queue.db`). Running counts and capacity still reflect that
  home’s DB / live processes when the daemon is up.

### Cursor (global)

Edit `~/.cursor/mcp.json` (Windows: `%USERPROFILE%\.cursor\mcp.json`). Cursor
uses the top-level key `mcpServers`:

```json
{
  "mcpServers": {
    "tqdaemon": {
      "command": "tq",
      "args": ["mcp"],
      "env": {
        "TQDAEMON_HOME": "C:\\Users\\YOU\\.tqdaemon",
        "TQ_OPERATOR": "YOU"
      }
    }
  }
}
```

Reload MCP servers / restart Cursor after saving. Prefer this user-level file
for tqdaemon. Workspace `.vscode/mcp.json` in some checkouts uses a different
schema (`"servers"`) and may already host other tools (e.g. repowise) — add
tqdaemon there only if you want a project-scoped override.

### Claude Code

Register from a shell (user scope, available in every project):

```bash
claude mcp add tqdaemon --scope user \
  -e TQDAEMON_HOME="$HOME/.tqdaemon" -e TQ_OPERATOR=YOU -- tq mcp
```

Or commit a project-scoped `.mcp.json` at the repo root (same `mcpServers` shape;
`claude mcp add --scope project` writes it). Then run `/mcp` in the session and
confirm `tqdaemon` is connected; tools appear as `mcp__tqdaemon__*`.

```json
{
  "mcpServers": {
    "tqdaemon": {
      "command": "tq",
      "args": ["mcp"],
      "env": {
        "TQDAEMON_HOME": "C:\\Users\\YOU\\.tqdaemon",
        "TQ_OPERATOR": "YOU"
      }
    }
  }
}
```

### Daemon home

| Target | `TQDAEMON_HOME` | UI port (daemon) |
|--------|------------------|------------------|
| Default | omit (uses `~/.tqdaemon`) | `8765` |

Point MCP at the same home the overnight daemon uses. Confirm with `tq doctor` / MCP `queue_status`.

### Verify

After reload, call MCP `queue_status` and confirm `home` / counts match
`tq doctor`:

```bat
tq doctor
```

Set `TQ_OPERATOR` in the MCP env (or pass `operator` on each `my_tasks` call)
so attribution filters work.

## Tools

Most tools are read-only; the write tools (`submit_task`, `submit_matrix`,
`enqueue_task` / `retry_task`, `patch_task`, `stop_task`) perform the same
operations as the CLI equivalents. Call `tools/list` to get the live schema;
the table below is a quick reference.

## Tools by capability

Use this map to choose a tool. Full schemas and examples follow below.

- **Submit:** `submit_task`, `submit_matrix`, `list_projects`
- **Admit:** `capacity`, `why_not_running`
- **Run:** `enqueue_task`, `retry_task`, `retry_tasks`, `patch_task`, `stop_task`, `hold_task`, `unhold_task`
- **Wait / notify:** `wait_for_task`, `drain_notifications`
- **Diagnose:** `diagnose`, `explain_error`, `task_result`, `report`
- **Observe:** `my_tasks`, `attention_list`, `task_health`, `queue_status`,
  `progress_snapshot`, `log_tail`, `agent_usage_summary`
- **Workers, auth, admin:** use the CLI and HTTP guides; MCP does not expose
  worker registration or configuration-admin tools.

### `my_tasks`

All tasks for one operator (matches the `started_by` column set via
`TQ_OPERATOR` env or `tq add`'s attribution).

| Arg | Required | Notes |
|-----|----------|-------|
| `operator` | one of `operator` or `TQ_OPERATOR` env must be set | falls back to the `TQ_OPERATOR` env var of the process running `tq mcp` if omitted |
| `project` | no | filter to one project |

Raises (`tools/call` returns a JSON-RPC error) if neither `operator` nor
`TQ_OPERATOR` is set.

```json
{"method": "tools/call", "params": {"name": "my_tasks", "arguments": {"operator": "alice"}}}
```

→ `{"tasks": [...], "count": N}` — each task row as stored in the `tasks` table.

### `capacity`

Cluster capacity snapshot — same payload as `GET /api/capacity` / `tq capacity`
(master + workers free CPU/RAM/GPU, per-device `gpus[]`, current reservations).

```json
{"method": "tools/call", "params": {"name": "capacity", "arguments": {}}}
```

No arguments.

### `attention_list`

Tasks needing a human/agent look: errored, failed, interrupted, suspended,
or related attention codes (may also include queued rows with block reasons).

| Arg | Required | Notes |
|-----|----------|-------|
| `operator` | no | filter to one operator |
| `project` | no | filter to one project |
| `since` | no | ISO timestamp lower bound on `updated_at` |

**Cleanup triage:** act on real terminals (`failed` / `interrupted` /
`suspended` / `stopped`). Ignore pure waits (`tq.block.resources`,
`tq.block.deps`, …) — those are not failures. See operator queue-hygiene notes in
the docs hub.

```json
{"method": "tools/call", "params": {"name": "attention_list", "arguments": {"project": "demo"}}}
```

→ `{"items": [...], "count": N}`.

### `task_health`

Submitted-task health report (same payload as `tq health --json`): status
counts, broken/missing `depends_on` peers, attention items, and disabled tasks.

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | no | filter to one project |

```json
{"method": "tools/call", "params": {"name": "task_health", "arguments": {"project": "demo"}}}
```

→ `{"counts": {...}, "broken_deps": [...], "missing_deps": [...], "attention": [...], "disabled": [...], "ok": bool}`.

### `why_not_running`

Why a task is (not) running: the scheduler's last `block_reason` / `last_error_code`, an unmet dependency, a paused or frozen project, `max_attempts`, and for queued tasks a dry-run verdict from `build_plan` (approximate: services, mutexes and the P6 hold are not simulated). Same as `tq why project/task --json` and `GET /api/tasks/<project>/<task_id>/why`.

| Arg | Required |
|-----|----------|
| `project` | **yes** |
| `task_id` | **yes** |

→ `{"ok": true, "status": "queued", "reasons": ["waiting on dependency p/up"], "plan": {"verdict": "blocked", "reason": "..."}}`.

### `diagnose`

Deep bundle for one task: recent runs, log tail, worker/lease info, plus a
`metrics` object from the shared builder — the MCP equivalent of
`tq diagnose project/task_id`.

### `explain_error`

Read-only lookup in the stable `tq.*` error catalog. It does not require a
queue database.

| Arg | Required |
|-----|----------|
| `error_code` | **yes** |

Returns `{ok, known, code, description, block_kind}`. The CLI equivalent is
`tq explain <error_code>`.

| Arg | Required |
|-----|----------|
| `project` | **yes** |
| `task_id` | **yes** |

```json
{"method": "tools/call", "params": {"name": "diagnose", "arguments": {"project": "demo", "task_id": "hello-progress"}}}
```

### `report`

Aggregate runs/error-code report over recent days — the MCP equivalent of
`tq report`. Pass `kind=hygiene` for churn rates, multi-use / flaky lists, and
archive proposals (`agent_prompt` included). Hygiene never archives; proposals
are dry-run commands only.

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | no | omit for all projects |
| `days` | no | default `7` |
| `kind` | no | `summary` (default) or `hygiene` |
| `older_than_days` | no | hygiene archive age (default `30`) |

```json
{"method": "tools/call", "params": {"name": "report", "arguments": {"days": 14}}}
```

```json
{"method": "tools/call", "params": {"name": "report", "arguments": {"kind": "hygiene", "days": 7}}}
```

### `queue_status`

Compact daemon summary: home path, port, protocol version, schema version,
task counts by status. Good first call to sanity-check which queue you're
actually talking to.

```json
{"method": "tools/call", "params": {"name": "queue_status", "arguments": {}}}
```

No arguments.

### `drain_notifications`

Read task-end events from `{TQDAEMON_HOME}/notify.jsonl` (written for **every**
terminal task — `notify_url` in the YAML only adds a webhook; see
[`task_standards.md`](task_standards.md) — and also when a **system alert** fires, with `status` like `alert:any_failure` /
`alert:tq_stopped`; see [Alerts](help/page-alerts.md)).
Does **not** delete the log; pass `since` (ISO `ts` from a prior event) as a
cursor. MCP is request/response — it cannot push-wake an agent session. To be
told that a specific task finished use [`wait_for_task`](#wait_for_task);
use this tool for webhook-style events (`notify_url`) and alerts.

| Arg | Required | Notes |
|-----|----------|-------|
| `since` | no | ISO timestamp lower bound on event `ts` |
| `operator` | no | events where `started_by` **or** `submitted_by` equals it |
| `project` | no | filter to one project |
| `wait_sec` | no | long-poll: if nothing new, re-check every 0.5 s for up to N seconds (cap 100) |

Every other tool response also carries `finished_since_last_call` (compact terminal events for `TQ_OPERATOR` since the previous call of this MCP process) when something finished — a free completion signal on calls you make anyway. It is omitted from `wait_for_task` / `drain_notifications`, and nothing is replayed from before the MCP process started.

```json
{"method": "tools/call", "params": {"name": "drain_notifications", "arguments": {"operator": "alice"}}}
```

→ `{"events": [...], "count": N, "path": ".../notify.jsonl"}`.

### `list_projects`

Registered projects with `queue_dir` and `root` paths. No arguments. → `{"projects": [{"name", "queue_dir", "root"}], "count": N}`.

### `wait_for_task`

Block until task(s) **settle**. Preferred way to learn that a task finished — no
`notify_url` needed. Settled = `completed`, `failed`, `interrupted`, `stopped`,
`suspended`, `unknown` (finished) or `disabled` (will not run; `finished` is false).
If a selected task existed when wait started and is then deleted (`tq remove` /
YAML sync), wait settles it as `status: "removed"` with `last_error_code:
tq.wait.removed` (not `not_found` — that is only for keys never in the DB).

| Arg | Notes |
|-----|-------|
| `project` + `task_id` | one task |
| `tasks` | list of `"project/task_id"` keys |
| `project`, `suite`, `group`, `tag`, `mine` | filters; wait only for tasks that have **not** settled yet (`mine` = started by `TQ_OPERATOR`). Add `include_finished` to include settled rows |
| `mode` | `all` (default) or `any` |
| `timeout_sec` | default `60`, **hard cap ~100 s** (`TQ_MCP_WAIT_MAX_SEC` in the MCP env to change). Longer waits: call again while `timed_out` is true |
| `poll_sec` | max poll interval (default `5`; starts at 1 s and backs off) |

→ `{"ok", "done", "timed_out", "waited_sec", "tasks": [{"key", "status", "finished", "settled", "exit_code", "last_error_code", "block_reason", "stop_reason", "wall_sec", "progress_pct", "eta", "last_lines", "next_actions", "log_tail"?}], "pending": [...], "not_found": [...]}`.
`eta` is described [below](#eta); on a timeout with one task still pending, the
`hint` repeats its text (`still running; call wait again (~1h15m00s left (rate))`).
`log_tail` (last ~2 KB) is inlined for settled non-completed tasks. With a single
task the legacy flat fields `status` / `exit_code` / `last_lines` are also set.
The wait runs on its own thread, so other tools stay responsive, and clients that
send a `progressToken` get `notifications/progress` every ~10 s. Call
`task_result` afterwards for metrics and artifacts.

`submit_task` / `submit_matrix` accept `wait: true` (+ `timeout_sec`) to submit
and wait in one call; the result gains a `wait` object in the shape above.

### `agent_usage_summary`

Usage analytics (counters only, no payloads — see [security.md](security.md#usage-analytics)).

| Arg | Notes |
|-----|-------|
| `days` | window, default `7` |
| `by` | interaction dims, comma list of `channel,client,operator,project,op,error_code,day,hour` (default `channel,client`) |
| `task_by` | submission dims, comma list of `via,by,client,project,day` (default `via,by`) |
| `project`, `channel`, `operator`, `limit` | filters |

→ `{"usage": [{<dims>, "calls", "errors", "avg_ms", "max_ms"}], "tasks": [{<dims>, "tasks", "completed", "failed", "interrupted_or_stopped", "active", "retries", "wall_sec_p50", "wall_sec_p95", "top_errors"}], "tracking_enabled": true}`. CLI: `tq agents`; HTTP: `GET /api/agent-usage`. `errors` excludes `wait.*` outcome codes (`wait.timeout`, `wait.task_failed`); group by `error_code` to see them (see [security.md](security.md#usage-analytics)).

### `task_result`

Structured result for a task: status, `exit_code`, duration, peak resources,
`TQ_METRIC` metrics, artifacts, `error_code`, `block_reason`, [`eta`](#eta).
Call it after `wait_for_task` returns.

| Arg | Required |
|-----|----------|
| `project`, `task_id` | **yes** |

### `log_tail`

Byte-range tail of a task's latest log (running or recent). Prefer incremental
reads with `next_offset` as the next `offset`.

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | **yes** | |
| `task_id` | **yes** | |
| `offset` | no | Byte offset from start; default = read last `max_bytes` from end |
| `max_bytes` | no | Max bytes to return (default `32768`) |
| `grep` | no | Optional substring filter on returned text |

```json
{"method": "tools/call", "params": {"name": "log_tail", "arguments": {"project": "myproj", "task_id": "train_001", "max_bytes": 16384}}}
```

→ `{"text": "...", "size_bytes": N, "offset": N, "next_offset": N, "log_path": "...", "stream_done": bool}`. `stream_done` is true only when the run is finished and `next_offset` reached the end of the file.
There is **no** `lines` argument — that was a stale schema.
### `progress_snapshot`

Current progress and resource metrics for one or more tasks (same fields as the
UI metrics dialog / `tq metrics`).

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | **yes** | |
| `task_id` | no | omit for all running tasks in project |
| `include_history` | no | default `false`; when `true`, include recent `resource_history` samples |
| `samples` | no | history window size (default `60`) when `include_history` |

```json
{"method": "tools/call", "params": {"name": "progress_snapshot", "arguments": {"project": "myproj"}}}
```

→ `{"tasks": [{task_id, run_id, progress_pct, eta_sec, eta, last_line, elapsed_sec, status, requires_gpu, log, resources, history_summary, metrics, source}, ...]}`.
`eta_sec` is `eta.remaining_sec`; for a run that already ended, `elapsed_sec` is its wall time.

`requires_gpu` is true when the task declares `gpu_vram_gib > 0`. `log` is
`{warnings, errors, other}` (zeros when none). `resources.current` / `peak`
cover CPU/RAM/VRAM; `resources.avg.gpu_util_pct` is mean/EMA GPU util when
sampled. Local GPU VRAM includes sidecar processes named in `locks` /
`requires_services`. `source` is `db` or `db+http` when the live daemon status
overlay was applied.

---

## Write tools

These tools mutate queue state. Use with the same care as the CLI equivalents.

### `submit_task` — create a new task (preferred MCP path)

Post full task YAML to the master (`/api/cli/add`) **without** writing a file first.
Equivalent to `tq submit` / `tq add` with inline YAML. Use this when the agent is
MCP-first and should not depend on shell/`tq sync`.

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | **yes** | Registered project name (`list_projects`) |
| `yaml_text` | **yes** | Full YAML body (`task_id`, `command`, …) — see `docs/task_standards.md` |
| `filename` | no | Hint; default `submitted_*.yaml` under the project's queue dir |
| `wait` | no | If true, also run `wait_for_task` logic; result gains a `wait` object |
| `timeout_sec` | no | With `wait`: max seconds (default 60, cap ~100) |
| `client_key` | no | Idempotency key (≤128 chars, per project). Retrying after a timeout with the same key and body returns the first submission's `task_ids` with `deduplicated: true` instead of a second task file. Same key, different body → error ("different submission"); a retry while the first is still being written → error ("still in progress"), retry shortly. Keys expire after `submit_key_ttl_sec` (default 24 h). |

→ `{"ok", "task_ids", "errors": [{"field","message"}], "deduplicated"?, "next"?}`. On success,
`next` is a ready-made `wait_for_task` call (unless you already passed `wait`).

### `submit_matrix` — create many variants

Same as `submit_task`, but builds a matrix from `base_yaml` + `params` list
(injected as `matrix: [...]`). Returns `{ok, task_ids, errors}`. Also accepts
`wait` / `timeout_sec` and `client_key`.

### `enqueue_task` / `retry_task` — re-queue an existing terminal task

Re-queue a task that **already exists** and has reached a terminal state (`failed` / `interrupted` / `stopped` / `completed`). Equivalent to `tq retry project/task_id`. `retry_task` is an alias.

**Did the re-run happen?** `retry_task` returns `run_number_before`. `wait_for_task` entries and `task_result` carry `run_number` (1-based count of runs of that task, 0 = never ran), `last_run_started_at` and `last_run_finished_at`. The same fields are on `GET /api/tasks/<p>/<t>`, `tq describe`, and the REST retry response. The re-run has started once `run_number > run_number_before`; while it is still `queued`, those fields (and `exit_code`) describe the previous run.

Wall-clock cap hits land as `interrupted` + `tq.run.cap`. Checkpointable tasks may set YAML `auto_retry_on_cap: true` so the daemon re-queues without an MCP/CLI call; otherwise call `retry_task` after a cap.

> **`enqueue_task` / `retry_task` cannot create a new task.** To create one, use
> [`submit_task`](#submit_task--create-a-new-task-preferred-mcp-path) or write a
> YAML under the project's queue dir and `tq sync` — see
> [Creating a new task](#creating-a-new-task-agent-workflow).

| Arg | Required |
|-----|----------|
| `project` | **yes** |
| `task_id` | **yes** |

→ `{"ok": true/false, "status": "queued"}`.

### `retry_tasks` — preview or retry a selected batch

Select with `tasks`, `project`, `suite`, `group`, or `downstream_of`. Set
`dry_run: true` (the default) to return per-task `retry`/`skip` actions without
changing queue state. With `dry_run: false`, the selected tasks use the same
daemon retry-batch/downstream operations as the CLI. `suite` includes completed
members; project/group selection defaults to terminal retryable tasks other
than completed.

### Pipelines: branch and batch (agent patterns)

MCP does not expose suite/group retry or `--downstream` yet — use CLI for those. For branching after notify:

1. **Sequence / check:** YAML `depends_on` (+ optional check task). No MCP needed to start the next step once deps complete.
2. **Choose B or C:** pre-author both with `enabled: false`; after `drain_notifications`, `patch_task` with `{"enabled": true}` on **one** winner (or `enqueue_task` / `retry_task` if already terminal).
3. **Re-run a bunch:** CLI `tq suite retry <suite>` / `tq group retry <project> <group>` (includes completed) or `tq retry project/root --downstream`.

See [`task_standards.md`](task_standards.md) § Dependency Management and [devkit queue rules](https://github.com/wwylon/devkit/blob/main/rules/queue.md) § Pipelines.

### `patch_task`

Update task fields without editing the YAML file. Supported fields: `priority`,
`enabled`, `wall_clock_cap_sec`, `max_attempts` (not `description`).

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | **yes** | |
| `task_id` | **yes** | |
| `updates` | **yes** | dict of field → value, e.g. `{"max_attempts": 0}` |

→ `{"ok": true, "applied": {"max_attempts": 0}, "message": "..."}`.

Use `max_attempts: 0` (or a higher budget) when a task is stuck after
`max_attempts=N reached` (status `failed`), then `retry_task` / `tq retry`.
Setting `enabled: true` on a **terminal** task (`completed` / `failed` / …) does **not** re-queue it — call `retry_task` / `tq retry`. Prefer CLI `tq result project/task` for the final log line (no MCP equivalent yet); `tq list --json` exposes `enabled` and `progress_pct`.

### `stop_task`

Request cooperative stop of a running task (writes control `suspend`). The task must honour the control file / SIGTERM after grace — it is not force-killed. Equivalent to `tq stop project/task_id`. A **queued** task is cancelled instead (status `interrupted`), like the web UI Stop; that needs the daemon to be reachable.

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | **yes** | |
| `task_id` | **yes** | |
| `reason` | no | Stored on `runs.stop_reason` (max 200 chars); shown in drawer / `task_result` / `why_not_running` |

→ `{"ok": true/false, "status": "...", "message": "...", "eta"?}`. `eta` is the
estimate at the moment of the stop (tasks are often stopped for being too slow);
`tq stop` prints it as `ETA at stop: …`.

### ETA

`wait_for_task`, `task_result`, `progress_snapshot` and `stop_task` carry an
`eta` object (or `null` when there is nothing to estimate from), built from the
task's latest run:

| Case | Fields |
|------|--------|
| running | `remaining_sec`, `finish_at` (UTC ISO), `basis`, `progress_pct`, `text` (`~16m40s left (rate)`) |
| last run ended before finishing (stopped / interrupted / suspended / failed) | `remaining_sec`, `at_stop: true`, `stopped_at`, `basis`, `progress_pct`, `text` (`was ~2h05m00s from done at 25% when the run ended (stopped, rate)`). It does not change afterwards. |
| any, when completed runs exist | `typical_sec` (median wall time of the last 10 completed runs) |

`basis` is `rate` (recent `N%` progress rate), `average` (run time / progress, when
only one progress line was seen) or `history` (`typical_sec` minus time spent, for
tasks that print no progress). Nothing extra is stored: the estimate at the stop
is derived from the run's progress, wall time and `metrics_json._progress`, so it
also covers runs stopped before this field existed.

### `hold_task`

Ask a **running** task to idle in-process for a while (control `hold` + `until`). Keeps RAM/GPU reserved — not project freeze, not suspend/exit 75. The task must poll `TQ_CONTROL_FILE` (`tq_client.maybe_hold` / `maybe_suspend`). Equivalent to `tq hold project/task --for SEC`.

| Arg | Required | Notes |
|-----|----------|-------|
| `project` | **yes** | |
| `task_id` | **yes** | |
| `duration_sec` | no | default `600` |
| `reason` | no | optional operator note |

### `unhold_task`

Clear a cooperative hold so work can resume. Equivalent to `tq unhold project/task`. Do not call unless the owner asked.

| Arg | Required |
|-----|----------|
| `project` | **yes** |
| `task_id` | **yes** |

---

## Errors

- A tool that fails returns a normal result with `"isError": true` and the message in `content[0].text`, so the agent sees it. Missing required arguments are reported the same way.
- Protocol errors use JSON-RPC codes: `-32700` (line is not JSON), `-32600` (not a request object), `-32601` (unknown method), `-32602` (unknown tool or `arguments` not an object).

## Creating a new task (agent workflow)

Two supported paths (same YAML contract — `docs/task_standards.md`):

### A. MCP `submit_task` (preferred when MCP is configured)

1. **`list_projects`** — pick `project` (and note `root` / `queue_dir`).
2. **`submit_task`** with `project` + `yaml_text` (include `task_id`, `command`,
   `args`, `wall_clock_cap_sec`; set `enabled: true` unless you mean to park it).
3. Follow the returned `next` → **`wait_for_task`** (short `timeout_sec`, repeat
   on `timed_out`), then **`task_result`**.

```json
{
  "name": "submit_task",
  "arguments": {
    "project": "myproject",
    "yaml_text": "task_id: my-task\ncommand: python\nargs: [scripts/train.py]\nwall_clock_cap_sec: 3600\nenabled: true\n"
  }
}
```

### B. File on disk + `tq sync` (CLI / editors)

1. **Find the project root.** MCP `list_projects`, or CLI `tq projects`, or
   `~/.tqdaemon/projects.yaml`.
2. **Write** `<project_root>/<queue_dir>/<task_id>.yaml` (default queue dir
   `experiments/operator_queue/`). Set `enabled: true`.
3. **`tq sync`** (or wait up to ~60 s for auto-sync).
4. **Confirm** with `queue_status` / `my_tasks` / `tq list`.

**Do not confuse with `enqueue_task`:** that tool only re-queues an *existing*
terminal task (`tq retry`). It cannot create.

**Attribution (`started_by`):** set `TQ_OPERATOR` in the MCP/CLI environment
before submit/sync. If unset, `started_by` is empty and `my_tasks` will not find
your tasks — use `attention_list` with a `project` filter as fallback.

**Waiting for completion (preferred):** after create/retry, call
`wait_for_task(project, task_id, timeout_sec=60)`; if `timed_out`, call it
again; when settled call `task_result`. CLI equivalent:
`tq wait project/task_id [more keys | --suite S | --group G | --tag T | --mine] [--any] [--timeout N] [--json]`
(blocks; exit 0 all completed, 1 failed/stopped/…, 2 timeout, 3 not found; the
task's own exit code is in `--json` or `tq result`; `--raw-exit` restores the
old single-task behaviour). In Claude Code, run `tq wait …` with
`run_in_background` and the session is re-invoked when the task ends — this is
a watcher, not a long job, so it does not violate the "no background shell
jobs" rule.

`submit_task` / `submit_matrix` (without `wait`) and `retry_task` /
`enqueue_task` (on success) return a `next` object:
`{"tool": "wait_for_task", "arguments": {...}, "cli": "tq wait … --timeout 3600", "note": …}`.
The CLI prints the same as a `Next: tq wait …` line on **stderr** after
`tq sync`, `tq add` / `tq submit` and `tq retry`; set `TQ_NO_HINTS=1` to hide it.

**Reading the event log (no `notify_url` needed):**
```
# First call — no since cursor
result = drain_notifications(project="myproject", operator="myop")
# Subsequent calls — pass last seen ts to avoid re-reading
result = drain_notifications(project="myproject", operator="myop", since=last_ts)
```
Events exist for every terminal task unless `notify_default=0`; in that case fall back to `my_tasks` / `wait_for_task`. The incremental cursor is stored per `operator` in `notify_cursor.json`, so two sessions using the same operator share it — use `since` for an independent cursor.

---

## Not implemented (yet)

There are no `attention_for_operator` / `watch_attention` aliases. Use
`attention_list` with an `operator` filter, and `drain_notifications` (with
`wait_sec`) for terminal events.

## See also

- [`docs/task_standards.md`](task_standards.md) — YAML fields (incl. `notify_url`), error codes, suspend contract.
- [`docs/errors.md`](errors.md) — full `tq.*` error code catalog referenced by `attention_list`/`diagnose`.
- [`docs/agents/recipes.md`](agents/recipes.md) — agent queue recipes.
