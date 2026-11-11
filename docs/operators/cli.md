# CLI reference

Full `tq` command table and capability groupings. Install and quickstart: [root README](../../README.md). MCP map: [capabilities.md](../capabilities.md).

## Capability groups

Start with the capability you need:

- **Submit:** `tq add`, `tq submit`, `tq clone`, `tq validate`, `tq sync`
- **Admit:** `tq plan`, `tq why`, `tq force`, `tq priority`, `tq locks`,
  `tq fair-share`, `tq pause`, `tq resume`, `tq freeze`, `tq unfreeze`
- **Run:** `tq start`, `tq stop-daemon`, `tq restart`, `tq retry`, `tq stop`,
  `tq suspend`, `tq patch`, `tq reverify`, `tq remove`, `tq group`, `tq suite`,
  `tq tag`
- **Wait / notify:** `tq wait`, `tq watch`, `tq tail`, `tq result`, `tq logs`
- **Diagnose:** `tq diagnose`, `tq explain`, `tq metrics`, `tq report`,
  `tq diff`, `tq health`
- **Workers:** `tq worker`, `tq projects`, `tq set`, `tq resync`, `tq bundle`,
  `tq master-status`, `tq master-migrate`
- **Auth:** `tq token`, `tq config`, `tq mcp`
- **Observe:** `tq status`, `tq list`, `tq describe`, `tq top`, `tq capacity`,
  `tq my-tasks`, `tq agents`, `tq doctor`, `tq log`, `tq detect`, `tq usage`
- **Admin:** `tq archive`, `tq prune`, `tq export`, `tq import`, `tq migrate`,
  `tq completion`, `tq sleep`, `tq wake`, `tq release`, `tq relocate`

See the [capability guide](../capabilities.md) for the MCP equivalent and
the full ownership map.

### Detailed CLI reference

Exit codes: `0` ok, `1` error, `2` usage (unknown flags or extra args are rejected, nothing is executed), `3` master unreachable.

| Command | Purpose |
|---------|---------|
| `tq start` | Start daemon (background, detached) |
| `tq completion bash\|zsh\|pwsh` | Print a shell completion script (commands, subcommands, flags), e.g. `tq completion bash >> ~/.bashrc` or `tq completion pwsh >> $PROFILE`. `tq <command> --help` prints that command's usage without running it |
| `tq stop-daemon [--force]` | Graceful shutdown; refuses while tasks are running (they would be stopped and re-queued from scratch) unless `--force` |
| `tq restart [--timeout=SEC] [--force]` | Stop, wait for exit, then start (default wait 180s); refuses while tasks are running unless `--force` — except runs started with `adopt_on_restart=1`, which keep running and are adopted by the new daemon ([scheduler.md](../scheduler.md#adopting-running-tasks-across-a-restart)) |
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
| `tq release <project>` | Drop the daemon's queue-dir file watch so the project folder can be renamed on Windows (F-240) |
| `tq relocate <project> <new_root>` | After a folder move: rewrite `projects.yaml` `root:`, backup `queue.db`, rewrite `yaml_source`, re-watch, sync (F-240) |
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
| `tq mcp` | Stdio MCP for agents — **setup** (Cursor/Claude) + tools in [`docs/mcp.md`](../mcp.md); opt-in YAML `notify_url` in [`docs/task_standards.md`](../task_standards.md) |
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
| `tq projects` | List registered projects (includes `set=` path when assigned) |
| `tq set tree` | Print multilevel project-set tree + counts (from `projects.yaml` `set:`) |
| `tq set show <path>` | Subtree members + frozen/paused |
| `tq set assign <project> <path>` / `tq set clear <project>` | Assign or clear a project's set path |
| `tq set rename <old> <new>` / `tq set move <old> <new>` | Rewrite every `set:` equal to `old` or under `old/` |
| `tq set limits <path> [field=val…]` / `tq set limits-show <path>` | Set-level limit defaults (inherited by members) / show defaults + effective |
| `tq set freeze\|unfreeze\|pause\|resume\|stop <path>` | Fan-out to every project in the subtree (distinct from task `tq group`) |
| `tq bundle <project>` | Pack project binary bundle for workers |
| `tq worker list` | List workers |
| `tq worker remove <worker_id>` | Deregister worker |
| `tq worker setup --master <url>` | Clone projects / init venvs on worker |
| `tq worker --master <url> --worker-id <id>` | Start worker client |
