# Error codes — taskqueue

Stable machine-readable codes stored in `tasks.last_error_code` (and optionally `runs.last_error_code`). Human text may also appear in `tasks.block_reason`.

Prefix: `tq.<domain>.<name>`.

## `tq.block.*` — cannot start yet

These appear as `block_reason` on `queued` tasks, not on runs.

| Code | Short label | Meaning | Action |
|------|-------------|---------|--------|
| `tq.block.deps` | Waiting on dependencies | `depends_on` not all `completed` | Wait, or use `tq force` to bypass |
| `tq.block.resources` | Waiting on resources | Host over soft RAM / CPU/GPU budget or anti-flap | Wait for resources to free; `tq force` to bypass |
| `tq.block.exclusive` | Waiting on exclusive gate | Another task is running, or this `exclusive` task waits | Wait for running tasks to finish |
| `tq.block.exclusive_project` | Waiting on project exclusive gate | Another same-project task running, or this `exclusive_project` task waits | Wait |
| `tq.block.requires_paths` | Required paths missing | `requires_paths` missing (also used as preflight on workers) | Ensure upstream task produces the paths |
| `tq.block.frozen` | Project blocked (frozen) | Project frozen via `tq freeze` / UI Block project — no new starts | `tq unfreeze <project>` |
| `tq.block.paused` | Project paused | Project paused via `tq pause` | `tq resume <project>` |
| `tq.block.mutex` | Waiting on named lock | Named `resources.locks` held by another running task | Wait; or rename lock if it is a config error |
| `tq.block.scavenger` | Waiting — capacity contended | A `class: scavenger` task waits because other (non-scavenger) work is blocked on capacity. | Self-clears when the queue is idle |

## `tq.run.*` — execution outcome

| Code | Short label | Meaning | Auto-retry? |
|------|-------------|---------|-------------|
| `tq.run.nonzero` | Non-zero exit | Process exited non-zero (not stop/cap) | Depends on `max_attempts` — `tq retry` manually |
| `tq.run.cap` | Hit wall-clock cap | Hit `wall_clock_cap_sec` (status `interrupted`). Opt-in auto-requeue: set `auto_retry_on_cap: true` on checkpointable/idempotent tasks | Opt-in via `auto_retry_on_cap`; else `tq retry` |
| `tq.run.preflight` | Preflight failed | Command or required path missing before start | Fix command / ensure path exists |
| `tq.run.frozen` | No output / idle process | No stdout **and** no process CPU for ≥2× `frozen_threshold_sec` (attention only; task keeps running) | No — manual investigation required |
| `tq.run.progress_suspect` | Progress suspect | Progress `%` advanced without matching process CPU time | No — check task's progress reporting |
| `tq.run.artifacts_missing` | Required artifacts missing | One or more declared `artifacts` paths absent after completion | No — fix task output path |
| `tq.run.negative` | Completed — negative result | Exit code listed in `negative_exit_codes`: the run worked and the answer is "no". Status is `completed`, not `failed`. | No — it is a result, not an error |

## `tq.stop.*` — scheduler eviction (auto-restartable)

| Code | Short label | Meaning | Auto-retry? |
|------|-------------|---------|-------------|
| `tq.stop.ram` | Stopped — host RAM hard limit | Host RAM exceeded `ram_hard_limit_pct`; task status `stopped`. Scheduler may restart when soft clears and anti-flap allows. | Yes — auto-restartable; `tq retry` / `tq force` also work |
| `tq.stop.vram` | Stopped — GPU VRAM hard limit | GPU VRAM exceeded `vram_hard_limit_pct`; task status `stopped`. Scheduler may restart when soft clears. | Yes — auto-restartable; `tq retry` / `tq force` also work |
| `tq.stop.scavenger` | Suspended — preempted for other work | A `class: scavenger` task was cooperatively suspended because non-scavenger work was waiting on capacity. Status `stopped`; restarts (with `resume_args`) once capacity is idle. | Yes — auto-restartable |
| `tq.dep.cancelled` | Cancelled — dependency failed | `on_dependency_failure: cancel`: a dependency failed, so this queued task was moved to `cancelled`. | `tq retry <root> --downstream` revives it |
| `tq.dep.negative` | Cancelled — dependency negative | `on_dependency_negative: cancel`: a dependency completed with a negative result. | `tq retry` if you want it to run anyway |
| `tq.stop.deps` | Stopped — dependency invalidated | A `depends_on` peer was re-queued or otherwise left `completed` while this task was still running. Task status `stopped`; re-admission waits until peers complete again, then may resume from checkpoint. | Yes — auto-restartable |

## `tq.infra.*` — platform / cluster

| Code | Short label | Meaning | Auto-retry? |
|------|-------------|---------|-------------|
| `tq.infra.worker_lost` | Worker lost — lease expired | Lease reclaim, and the worker's own heartbeat is also stale/offline at reclaim time | Yes (re-queued) |
| `tq.infra.lease_expired_worker_alive` | Lease expired (worker still online) | Lease reclaim, but the worker is still heartbeating — likely a stuck task on an otherwise-healthy worker, not a dead worker | No — investigate with `tq diagnose` |
| `tq.infra.clock_jump` | Host slept/woke — re-queued (only a real gap in the idle wait counts; slow ticks do not) | Sleep/wake orphan reclaim — task interrupted and re-queued | Yes (re-queued) |
| `tq.infra.clock_jump_flap` | Clock-jump flap | Repeated clock-jump interrupts exceeded `clock_jump_max_requeue` in the rolling window — task left `interrupted`, not auto-requeued | No — use `tq retry` when host is stable |
| `tq.infra.orphan` | Orphan run — re-queued | Local run left `running` after daemon death / dirty restart — interrupted and re-queued; see `daemon_events` / `tq doctor` | Yes (re-queued) |
| `tq.infra.hung` | Hung task — re-queued | Remote task showed no stdout progress and no `progress_pct` change for `hung_task_k` consecutive scheduler ticks (only fires for `idempotent: true` tasks) | Yes (re-queued for idempotent tasks) |

**Daemon lifecycle:** unclean exits are recorded in `daemon_events` (`dirty_restart`, `start`, `orphan_requeue`, `clean_shutdown`). Inspect with `tq doctor` or MCP `queue_status` (`last_boot`, `daemon_events`). Hard kills are detected on the next boot via a missing `daemon.clean` marker and/or a stale `daemon.pid`. The daemon handles SIGINT/SIGTERM/Ctrl+Break the same way: running task trees are stopped in parallel, then the pid file is removed and the lease released. Prefer `tq restart` over kill+`tq start`: clean shutdown releases `master_lease`, and a same-host reclaim skips waiting out the 600s TTL when the old PID is already dead.

Reclaim *timing* is unchanged either way — a run is reclaimed purely on lease expiry (`LEASE_TTL_SEC`, independent of worker heartbeat). The two `tq.infra.worker_lost` / `tq.infra.lease_expired_worker_alive` codes only change the *label*, distinguishing "the worker is fine, this task got stuck" from "the worker itself went dark" — surfaced in `tq diagnose` / `/api/capacity` / `tq capacity`.

**Tuning hung detection:** `tq config set hung_task_k=10` (default 5; 0 disables). Each tick is `scheduler_interval_sec` (default 5s), so the default triggers after ~25s of silence. Set higher for tasks with long silent compute phases. Per-task override: `hung_task_k: 60` in the task YAML.

**Remote disconnect triage:** see [master_worker.md](master_worker.md) — lease reclaim → `queued` vs `unknown`; pending-finalize outbox; `tq retry` for `unknown` tasks.

## CLI exit codes

Every `tq` command follows one contract (constants in `taskqueue/_cli_helpers.py`):

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | the command ran and the operation failed |
| 2 | usage error (bad or missing arguments) |
| 3 | prerequisite missing: daemon / master unreachable, no DB, or the named task / run was not found |

`tq wait` refines this (below): there `2` means `--timeout` elapsed. All `--json` output goes through one helper, so it is always valid JSON on stdout with diagnostics on stderr; `tq top --json` is the exception by design (NDJSON, one compact object per line).

## `tq wait` exit codes

| Code | Meaning |
|------|---------|
| 0 | every selected task `completed` |
| 1 | a settled task is `failed` / `interrupted` / `stopped` / `suspended` / `unknown` / `disabled` |
| 2 | `--timeout` elapsed with tasks still pending |
| 3 | task not found, no DB, or bad arguments |

`--raw-exit` (single task) restores the legacy codes: the task's own exit code, `124` on timeout, `1` if not found. With `--json` the task exit code is in `tasks[].exit_code`.

## HTTP API error responses

| Endpoint | Status | Body `error` | Condition |
|----------|--------|--------------|-----------|
| `POST /api/tasks/<project>/<id>/retry` | 409 | `"cannot retry task with status '<s>'"` | Task status is not `failed`, `interrupted`, or `stopped` |
| `POST /api/tasks/<project>/<id>/priority` | 409 | `"priority must be an integer"` | `priority` field not parseable as int |
| `POST /api/tasks/<project>/<id>/priority` | 409 | `"priority must be between 0 and 100"` | Value out of `[0, 100]` range |

## Checking errors

```powershell
# First step: always run tq doctor before any mutation
tq doctor

# Inspect a specific task
tq diagnose project/task_id

# List all tasks needing attention
tq attention

# Show recent failures and their error codes
tq report --days 7

# Full submitted-task health (counts, broken deps, attention, disabled)
tq health
tq health --json
```

In the web UI, the **Attention** panel surfaces tasks with non-null `block_reason` or recent `last_error_code`. Missing `depends_on` peers are painted red on the Queue tab with a banner.

## Surfaces

- CLI: `tq list` / `tq status` show `last_error_code` when set
- HTTP: task JSON includes `last_error_code` and `block_reason`
- Attention / MCP: filter by these codes (`attention_list`, `diagnose`)
