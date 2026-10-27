# Agent recipes

MCP-first workflows. Tool schemas: call `tools/list` on `tq mcp` or see [mcp.md](/docs/mcp).

**Hard constraints:** do not invent YAML keys; MCP cannot create new tasks (write YAML + `tq sync`); long jobs belong in operator queue YAMLs, not in-session shells.

## Session start

```json
{"name": "queue_status", "arguments": {}}
{"name": "capacity", "arguments": {}}
{"name": "attention_list", "arguments": {"project": "YOUR_PROJECT"}}
```

Set `TQ_OPERATOR` in MCP env so `my_tasks` attribution works.

## Create a new task

MCP **cannot** create tasks. Write a YAML to the project's queue directory and sync:

```powershell
# 1. Write YAML to <project_root>/experiments/operator_queue/<task_id>.yaml
# 2. Sync so the daemon picks it up
tq sync
# 3. Confirm it appeared
tq list --project myproject
```

Minimal YAML:
```yaml
task_id: my-task
command: python
args: [scripts/train.py]
wall_clock_cap_sec: 3600
```

Full field reference: [task_standards.md](/docs/task-standards).

## Monitor my work

```json
{"name": "my_tasks", "arguments": {"operator": "YOU"}}
```

## Diagnose → retry loop

```json
{"name": "diagnose", "arguments": {"project": "p", "task_id": "id"}}
{"name": "log_tail", "arguments": {"project": "p", "task_id": "id", "max_bytes": 32768}}
```

If terminal and budget allows: `retry_task` or `patch_task` then `retry_task`. Ignore pure `tq.block.*` waits — not failures.

## Re-run decision: retry vs reverify vs force

| Situation | Command |
|-----------|---------|
| Task failed/interrupted, want to re-queue | `tq retry project/task_id` |
| Chain restart: re-run a root and all dependents that depend on it | `tq reverify project/root_task --up-to project/leaf_task` |
| Task is blocked on resources but you know it is safe to run now | `tq force project/task_id` |
| Task is `max_attempts`-exhausted (`failed` with `max_attempts=N reached`) | `tq patch project/task_id max_attempts=0` then `tq retry` |

`retry` re-queues a single terminal task. `reverify` walks the dep chain, re-queues the root, and re-queues any downstream tasks whose state needs to be rebuilt. `force` bypasses resource gates only; the task must already be `queued`.

## Suite and group bulk operations

**Suite** — enable/disable/retry all members at once (does not re-queue `completed`):
```powershell
tq suite retry overnight-suite
tq suite disable overnight-suite
tq suite enable overnight-suite
```

**Group** — operational grouping for stop/retry of related experiments:
```powershell
tq group list
tq group stop my-experiment-group
tq group retry my-experiment-group
```

`suite` is for lifecycle management (YAML field `suite: "name"`). `group` is for bulk operational control (YAML field `group: "name"`). A task can have both.

## Long checkpointable jobs (auto-retry on cap)

For jobs that regularly hit `wall_clock_cap_sec`, set in YAML:
```yaml
execution_semantics: checkpointable
auto_retry_on_cap: true
max_auto_retries: 20   # default; 0 = unlimited
resume_args: [--resume, "{checkpoint_path}"]
wall_clock_cap_sec: 3600
```

The daemon automatically re-queues on cap hit and passes `resume_args` on the next start. When `max_auto_retries` is exhausted, the task stays `interrupted` with `block_reason=cap_auto_retries exhausted`; `tq retry` resets the counter.

{{topic:block-project}}
{{topic:force-run}}
{{topic:dependencies}}

## End-of-run notify

To block until a task finishes, use MCP `wait_for_task` (short `timeout_sec`, repeat on `timed_out`) then `task_result`, or CLI `tq wait project/task_id` (in Claude Code: `run_in_background`, which re-invokes the session on exit). No YAML change needed.

When YAML sets `notify_url` (external webhook), `drain_notifications` also returns the end-of-run event.

See sibling [devkit/rules/queue.md](../../devkit/rules/queue.md) for when to enqueue vs run in-session.
