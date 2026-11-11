---
id: cooperative-control
title: Stop, suspend, hold, freeze
summary: How operator stop reasons and cooperative control differ from project freeze.
ui_pages: [queue]
audience: [human, agent]
guide_anchors:
  web-ui: cooperative-control
---

**Scripts:** use [`tq_client.maybe_suspend` / `maybe_hold`](../../clients/README.md) (cookbook: [task-script-cookbook.md](../agents/task-script-cookbook.md)). Do not invent your own control-file polling.

TqDaemon has several “pause-like” controls. They are **not** interchangeable.

| Action | What it does | Process | Capacity |
|--------|----------------|---------|----------|
| **Stop** | Cooperative stop (control `suspend`, then SIGTERM after grace) | Ends | Freed |
| **Suspend** (`tq suspend`) | Control `suspend` only → task should exit **75** | Ends (`suspended`) | Freed |
| **Hold** (`tq hold` / UI Hold) | Control `hold` + `until` → task should **idle in-process** | Stays `running` | **Still reserved** |
| **Block project** (freeze) | No **new starts** in the project | Running continue | Unchanged |
| **Pause project** | Softer admission skip | Running continue | Unchanged |
| **FROZEN badge** | No recent log/CPU (idle detection) | Still running | — |

## Stop reason

When you **Stop** from the UI, pick a preset reason (owner request, gate lagging, wrong params, …) or Other + optional text. CLI: `tq stop project/task --reason gate:lagging`. MCP `stop_task` accepts `reason`.

The reason is stored on the latest run as `stop_reason`. TqDaemon auto-stops (RAM hard limit, wall-clock cap, frozen, deps invalidated, …) also set machine `stop_reason` / `stopped_by`.

Owners see why via:

- Drawer **Stop reason** / **Stopped by**
- `tq why` / MCP `why_not_running` (`stop_summary`)
- MCP `task_result` (`stop_reason`, `stopped_by`, `stop_summary`)

## Hold (in-process idle)

Use hold when you want the process to **do nothing for a while** but keep warm state (e.g. loaded model). Do **not** confuse with Block project.

- CLI: `tq hold project/task --for 600 --reason cooling` · `tq unhold project/task`
- MCP: `hold_task` / `unhold_task`
- UI drawer: **Hold** / **Resume hold**

The task must poll `TQ_CONTROL_FILE` (prefer [`tq_client`](../../clients/README.md) `maybe_hold` / `maybe_suspend`). Wall-clock caps still tick during hold.

Agents must not `unhold` (or unfreeze/resume a project) unless the owner asked.
