---
id: page-alerts
title: Alerts
summary: Built-in system events with Active and Windows popup toggles.
ui_pages: [alerts]
audience: [human]
guide_anchors:
  web-ui: page-alerts
---

**Alerts** lists five built-in system events. You cannot create or delete them. Each row has:

- **Active** — when on, TaskQueue evaluates / emits the event.
- **Windows popup** — when on, a Windows Action Center toast appears. Requires the optional [`winotify`](https://pypi.org/project/winotify/) package on the **same Python** that runs the daemon:

  ```text
  pip install winotify
  # or: pip install 'taskqueue[toast]'
  # or (project venv): uv sync --extra toast
  tq restart
  ```

  Toasts are colour-coded like the health light: a 🔴 title prefix and red icon for **Any failure** (and failed / unknown task-end toasts), 🟠 for **Task frozen**, **Stopped by TaskQueue** and **Unusual run**, 🟢 for **No more tasks to run**. Toast **title** names the event and task (for example `Task failed: proj/task`). The body gives the reason (error label, exit code, last output line) and ends with the `running · failed · queued` counts. `tq doctor` and daemon startup warn if Windows popup is on but `winotify` is missing.

- Without `winotify`, alerts still fire: **notification bell**, in-page toasts when the web UI is open, and **Fire count** / **Last fired** on this page. Events are also appended to `{TASKQUEUE_HOME}/notify.jsonl` with `status` like `alert:any_failure`.

**Alert history** (below the rules) lists every alert that fired, newest first. Click a row for the full detail: error label and code, exit code, attempt, elapsed time, worker, block reason and the last lines of the task log. The notification bell and in-page toasts show the same text (click a toast to open this page). Each alert toasts (and raises a browser notification, if allowed) once per browser: reloading does not repeat it, and alerts that arrived while the page was closed show on the next load. Every alert is also written to the daemon log as a line starting `[alert]`; suppressed alerts (rule off, cooldown) are logged at debug level under `taskqueue.alerts`. The structured detail is stored in the `detail` field of the `alert:*` event in `notify.jsonl`; `GET /api/notifications?kind=alert` returns only alerts.

| Alert | Meaning |
|-------|---------|
| **Any failure** | A run finishes with status `failed` (any project). |
| **No more tasks to run** | Nothing is `queued` or `running`, and at least one run finished recently. Quiet for one hour after it fires. |
| **Task frozen** | A running task has no stdout and no process CPU for ≥2× `frozen_threshold_sec` (same condition as the FROZEN badge). |
| **Stopped by TaskQueue** | An automated stop (`stopped_by=tq`), not a human/`tq stop`. The popup includes **why** (for example RAM hard limit, wall-clock cap, frozen, dependency invalidated). |
| **Unusual run** | A run finishes `completed` but its wall time, peak RAM or peak VRAM is above 1.5x the p95 of the last 20 completed runs of the same task (needs at least 5 earlier runs). The same runs get a ⚠ **Unusual** badge in History (hover for values). Toast colour: amber. |

The sidebar badge counts enabled alerts that are **hot now** (queue idle, or a currently frozen run). One-shot events (failure, TQ stop) do not keep the badge lit after the toast.

Related: task-end Windows toasts are controlled by config `toast_notifications` (see [WORKER_SETUP](../WORKER_SETUP.md)); that knob does **not** gate these system-alert popups.
