---
id: page-history
title: History
summary: Recent runs with run id, status, and drill-down.
ui_pages: [history]
audience: [human]
guide_anchors:
  web-ui: page-history
---

**History** lists recent execution attempts. Each row is **`#run_id`** for a project/task — use this id when discussing logs, metrics, or a specific failure.

The list loads **20** runs at a time (Load more fetches the next 20). Click **History** on a row for that task’s prior runs plus duration stats (average / P50 / P95 from completed runs). **Metrics** opens the resource chart for that run. **Re-run** (after a confirm) queues the task again with the arguments that run used (subject to the task's `overridable_args`); `tq clone` makes a new task from one instead. Click a row to inspect duration, exit status, and link to log search. Filter by project from the header selector. Use the search box to match **task id**, **project**, **`#run_id`**, error category, or worker id (server-side; works with Load more). `/` focuses search while this page is active.

## Comparing two runs

Click **⇄ Compare** on one run row (or the **⇄** button on a dashboard history card), then on a second run. A dialog lists only the fields
that differ: status, exit code, wall time, peak CPU/RAM/VRAM, worker, package hash, command,
effective arguments, environment variable **names** (values are never stored) and the numeric
metrics. The same data is available from `GET /api/runs/<id>` and `GET /api/runs/diff?a=&b=`.
Runs started before the upgrade have no argument/environment snapshot.

`GET /api/runs/<id>` also returns an `anomaly` block: peak RAM, peak VRAM or wall time more than 1.5x the p95 of the last 20 completed runs of the same task is listed in `anomaly.flags`. With fewer than 5 completed earlier runs nothing is flagged.

**Re-run with a past run's args.** `POST /api/runs/<run_id>/rerun` re-queues the run's task using the args recorded for that run. The recorded args replace the task's argv tail for one run, so they must stay within the task's `overridable_args`; otherwise the request is refused with 409 and nothing changes. The task must be in a retryable state. Requires admin scope.

Completed runs that exceeded the anomaly threshold (see `GET /api/runs/<id>`) show a ⚠ **Unusual** badge next to their status; hover it for the metric, value and the p95 baseline. `GET /api/history` returns the same flags as `anomalies` on each affected run, and the **Unusual run** alert (Alerts page) fires when such a run finishes.
