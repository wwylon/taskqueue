---
id: page-queue
title: Queue
summary: Filter, search, select, and act on tasks.
ui_pages: [queue]
audience: [human]
guide_anchors:
  web-ui: page-queue
---

The **Queue** page lists tasks with status chips, search, tags, and group chips. The header project filter limits the list and the four status counts. Those counts are totals for the project, not just the rows on screen. The **active** project chip shows every project except frozen or paused ones; queued tasks waiting on deps/resources (`tq.block.*`) stay visible.

- Changing the header project clears the selected **group** filter (groups belong to a project's task set). The Groups page **View in queue** link selects the group's project as well.
- Wait reasons under Status use color: muted for normal waits (resources, exclusive, healthy deps), warn for frozen/paused/service blocks, red when waiting on a dead dependency (`failed` / `interrupted` / `stopped` / missing).
- **Columns** hides optional table columns (priority, status, started, finished, resources, description); the choice is kept in this browser.
- **Saved filters** remember status, search, and project. **Failed today** only hides rows on the loaded page whose finish time is today.
- Selecting rows shows the bulk bar; **Select all N matching** extends the selection to every task matching the current filters (all pages, up to 5000). Bulk stop and retry send one request (`/api/tasks/bulk`, `/api/tasks/retry-batch`).
- **Retry all failed** retries the failed query and asks before more than 500. CSV and JSON export download the rows on screen.
- Running rows show **last progress N ago** once the % has not moved for a minute, and **⚠ possibly stalled** when it has not moved for over 3x the run's usual interval (at least 2 min) — a slow run stays below that (same rule as `tq status`). A task that exited with one of its `negative_exit_codes` shows a dashed **completed · no** badge: a negative result, not a failure.
- Cap hits show as **`interrupted`** with code **`tq.run.cap`** (often safe to retry or auto-retried via YAML `auto_retry_on_cap`). Real crashes show **`failed`** / **`tq.run.nonzero`**. CLI equivalent: `tq retry --project P --status failed,interrupted`.
- Select rows for bulk Retry, Stop, and Re-queue. **Force** is only offered on `queued` tasks.
- With a project selected, **Pause project**, **Block project** and **Stop All** sit in the filter bar. Pause keeps queued tasks waiting while running ones continue (`tq pause`); Block (freeze) makes the scheduler skip the project entirely.
- Click a task cell to open the drawer (info, YAML, last five runs). **Log** opens the bottom panel; its toolbar filters lines by regex (an invalid pattern matches literally), shows only warnings/errors or errors, toggles line wrap, and downloads the log (last 8 MiB). The drawer shows **Metrics (TQ_METRIC)** sparklines when the task's latest run printed numeric `TQ_METRIC` values (task-chosen keys — see [Metrics (TQ_METRIC)](tq-metric.md); not the same as the **📊 metrics** resource-usage modal). **Why not running?** lists what holds the task back: the scheduler's last block reason, an unmet dependency, a paused project, `max_attempts`, and for queued tasks a dry-run verdict (same as `tq why`).
- **Stop** waits 5 seconds and shows a toast with **Undo** before it is sent (a stopped run cannot continue mid-step).
- **Ctrl+K** (Cmd+K on macOS) opens the command palette on any page: type to jump to a page, open a task on the loaded queue page in the drawer, or refresh / show shortcuts.
- Shortcuts while this page is active and focus is not in a field: `j` / `k` move the row, `/` focuses search, `r` retries and `s` stops (both ask to confirm), `a` selects visible rows, Enter opens the drawer, `?` toggles the shortcut list, Esc closes the top layer (an open dialog first). Shortcuts are ignored with Ctrl/Alt/Cmd held, and Enter on a focused button activates the button instead of opening the drawer.
- **Shareable URLs** keep the section and filters: `page` (or legacy `tab`), `project`, `status`, `q`, `group`, `tag`, `doc`. A `tq_token` in an opened link is saved in the browser and then removed from the address bar, so shared URLs never carry it. Example: `/?page=queue&project=omnicard3&status=failed`. Browser Back and Forward restore them.
- A **lock contention** banner appears when queued tasks wait on a shared `resources.locks` mutex and the holder's project still has headroom under `max_tasks`.
- Queued tasks can also wait on **`exclusive`** (blocks all other tasks while it runs) or **`exclusive_project`** (blocks only same-project peers). Those show as `tq.block.exclusive` / `tq.block.exclusive_project` — Force does not bypass them.
- Live updates use **SSE** (`/api/events`) when the browser supports EventSource; the list refreshes within about a second of status changes. If SSE drops, the page falls back to a slower poll.

