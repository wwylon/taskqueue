# Capabilities

This page is the small map of tqdaemon's public surfaces. Use it to choose
the least surprising interface: `tq` for a human at a terminal, the web UI
for interactive observation, and MCP for an AI coding agent. The machine
readable version is [`surface_map.json`](surface_map.json).

## Feature themes (YAML + scripts)

When authoring a task, pick a theme in [task_standards §0](task_standards.md#0-yaml-field-reference) (Identity, Resources, Dependencies, Execution & resume, Dispatch, Schedule, Notify, Expansion). **Task scripts** go through [`tq_client`](../clients/README.md) — see [task-script-cookbook](agents/task-script-cookbook.md) and [`examples/`](../examples/).

## Submit

Create work from a YAML document or submit it directly through MCP.

- **MCP:** `submit_task` creates one task; `submit_matrix` creates variants;
  `list_projects` finds a registered project.
- **CLI:** `tq add` / `tq submit` submit YAML, `tq clone` copies a task as a
  template, `tq validate` checks YAML, and `tq sync` imports queue files.
- **Reference:** [Task Standards](task_standards.md), [Task script cookbook](agents/task-script-cookbook.md), [Agent recipes](agents/recipes.md).

## Admit

Admission decides whether a queued task can start on available CPU, RAM, GPU,
locks, dependencies, and project gates.

- **MCP:** `capacity` shows free resources; `why_not_running` explains a task's
  block and dry-run verdict.
- **CLI:** `tq capacity`, `tq plan`, `tq why`, `tq force`, `tq priority`,
  `tq locks`, `tq fair-share`, and `tq pause` / `resume` / `freeze` /
  `unfreeze`.
- **Reference:** [Scheduler](scheduler.md).

## Run

The daemon starts admitted tasks locally or on workers and supports cooperative
stop, retry, and task control.

- **MCP:** `enqueue_task` and `retry_task` re-queue existing terminal tasks;
  `retry_tasks` previews or retries suite/group/downstream selections;
  `patch_task` changes supported YAML fields; `stop_task` requests a
  cooperative stop.
- **CLI:** `tq start`, `stop-daemon`, `restart`, `retry`, `stop`, `suspend`,
  `hold`, `unhold`, `patch`, `reverify`, `remove`, `group`, `suite`, and `tag`.
- **MCP:** `stop_task` (optional `reason`), `hold_task`, `unhold_task`.
- **Important:** `enabled: true` does not re-queue a terminal task. Use
  `retry_task` or `tq retry`.

## Wait_notify (wait and notify)

Wait for settlement without scraping logs, then consume terminal events.

- **MCP:** `wait_for_task` blocks for one task or a filtered set;
  `drain_notifications` reads `notify.jsonl` incrementally and can long-poll.
- **CLI:** `tq wait`, `tq watch`, `tq tail`, `tq result`, and `tq logs`.
- **Reference:** [MCP wait workflow](mcp.md#creating-a-new-task-agent-workflow).

## Diagnose

Find out why work failed or what a run consumed.

- **MCP:** `diagnose` returns the deep bundle, `explain_error` looks up a
  stable error code, `task_result` returns the structured result, and
  `report` aggregates recent runs (`kind=hygiene` for churn / multi-use /
  archive proposals — never auto-archives).
- **CLI:** `tq diagnose`, `tq explain`, `tq metrics`, `tq report`,
  `tq hygiene` (alias for `tq report --hygiene`), `tq diff`, and `tq health`.
- **Reference:** [Error codes](errors.md).

## Workers

Workers are trusted machines that join a master and claim eligible tasks.
`dispatch: remote` prefers a worker but falls back locally when none is online.

- **CLI:** `tq worker`, `tq projects`, `tq set`, `tq resync`, `tq bundle`,
  `tq master-status`, and `tq master-migrate`.
- **Reference:** [Worker setup](WORKER_SETUP.md) and
  [Master-worker architecture](master_worker.md).

## Auth

Keep the daemon on loopback by default. Before a network bind, set a strong
`auth_token`; workers use that master credential. Scoped `tq_live_` tokens are
for operator API access and are separate from worker auth.

- **CLI:** `tq token`, `tq config`, and `tq mcp`.
- **Reference:** [Security](security.md) and [MCP setup](mcp.md).

## Observe

Inspect queue state, capacity, progress, logs, and usage from a terminal, the
web UI, or an agent.

- **MCP:** `my_tasks`, `attention_list`, `task_health`, `queue_status`,
  `progress_snapshot`, `log_tail`, and `agent_usage_summary`.
- **CLI:** `tq status`, `list`, `describe`, `top`, `capacity`, `my-tasks`,
  `agents`, `doctor`, `log`, `detect`, and `usage`.
- **Web UI:** Queue, Dashboard, History, Workers, Resources, Usage, Timeline,
  Alerts, Audit, and API pages are documented in
  [the web UI guide](operators/web-ui.md).

## Admin

Maintain homes, archives, retention, exports, imports, and shell completion.

- **CLI:** `tq archive`, `prune`, `export`, `import`, `migrate`, `completion`,
  `sleep`, `wake`, `release`, and `relocate`.

## Code ownership

This is an extraction guide, not a package reorganization:

| Question | Primary owner |
|---|---|
| CLI parsing and command surface | `tqdaemon/cli.py`, `_cli_*.py` |
| MCP schemas and dispatch | `tqdaemon/mcp_server.py` |
| Scheduling and admission | `tqdaemon/daemon.py`, `scheduler.py`, `resources.py` |
| Persistence and task queries | `tqdaemon/db.py`, `_db_*.py`, `queries.py` |
| HTTP API and auth | `tqdaemon/web/`, `tqdaemon/auth.py`, `docs/security.md` |
| Workers and remote execution | `tqdaemon/worker.py`, `_cli_worker.py`, `master_worker.md` |
| Static web UI | `tqdaemon/static/`, `docs/operators/web-ui.md` |
| Documentation registry and generated help | `docs/manifest.json`, `docs/help/`, `scripts/build_docs.py` |
| Surface drift enforcement | `docs/surface_map.json`, `scripts/check_docs.py`, `tests/test_docs_surface.py` |

