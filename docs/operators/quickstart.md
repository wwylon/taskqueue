# Quickstart

Install and run your first task in about ten minutes.

## Install

```powershell
git clone https://github.com/wwylon/taskqueue.git
cd taskqueue
uv sync
tq doctor
```

`tq doctor` prints your state home (`~/.taskqueue`), schema version, daemon pid, and port. Run it first whenever something seems off — it's the fastest way to confirm the daemon is up before issuing any commands.

## Register a project

Edit `~/.taskqueue/projects.yaml`:

```yaml
projects:
  myproject:
    root: D:\path\to\your\project
```

## First task

Copy [`examples/minimal/experiments/operator_queue/`](../../examples/minimal/) into your project, then:

```powershell
tq sync
tq status
```

The sample YAML ships with `enabled: false`. Enable it, then sync:

```powershell
tq patch myapp/hello-progress enabled=true
tq sync
```

(Or set `enabled: true` in the YAML before the first sync.)

If the task stays `waiting` with `tq.block.resources`, the host is over the soft RAM gate — check `tq why <project>/<task>` / `tq doctor`. On a busy machine you can temporarily raise the soft limit (`tq config set ram_limit_pct=95`) or free RAM.

> **Note:** `enabled: true` in a YAML **does not re-queue a task that has already finished** (`completed`, `failed`, `interrupted`, …). To re-run a terminal task, use `tq retry project/task_id`. Toggling `enabled` only affects non-terminal tasks (e.g. `disabled` → `queued`).

Watch the **Dashboard** tab or run `tq status` to follow progress.

## Create your own task

Write a YAML under your project's `experiments/operator_queue/` and run `tq sync`:

```yaml
# experiments/operator_queue/hello.yaml
task_id: hello
command: python
args: [scripts/hello.py]
description: "My first task"
wall_clock_cap_sec: 300
```

```powershell
tq sync
tq list --project myproject
```

MCP `enqueue_task` can only re-queue *existing* terminal tasks — it cannot create new ones. Always write a YAML + `tq sync` to create.

## Dispatch to a remote worker

`dispatch: remote` is a **prefer-remote hint**, not a hard requirement. If no worker is online, the task falls back to running on the master. Use `dispatch: any` to make the fallback explicit, or `dispatch: local` (the default) to force local-only.

```yaml
dispatch: remote
requires:
  os: linux
```

See [task_standards.md](/docs/task-standards) §4 for `requires` constraints and `dispatch: any`.

## Next steps

- [Web UI tour](/docs/web-ui) — same help text as the **?** button
- [Task standards](/docs/task-standards) — YAML field reference
- [Agent recipes](/docs/agents) — MCP workflows
- [Error codes](/docs/errors) — what `block_reason` and `last_error_code` mean
- [Examples](../../examples/README.md) — minimal and pipeline samples
