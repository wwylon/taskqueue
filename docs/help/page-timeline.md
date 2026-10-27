---
id: page-timeline
title: Timeline
summary: Forward forecast Gantt for queued and scheduled work.
ui_pages: [timeline]
audience: [human]
guide_anchors:
  web-ui: page-timeline
---

**Timeline** projects what is likely to run next. It is a **forecast**, not a replay of past runs.

Each bar is one queued, running, or soon-to-fire cron task. Bars share a forward time axis (`now` → the selected window: 6h, 24h, 72h, or 7 days). Overlapping bars mean the forecast expects parallel work under global and per-project concurrency limits (`max_running_tasks` / `max_concurrent_tasks`, project `max_tasks`, CPU/RAM/VRAM caps), plus `depends_on` / `exclusive` gates.

**Duration** comes from history when available (task p50), else similar task names in the same project, else the average of known task durations, else a fixed placeholder. Hover a bar for estimate source, lane, and kind. The header shows predicted **bunch end** (when the last projected bar finishes).

This is advisory: it does not model live host RAM pressure, remote worker binpack, or service probes. The header project filter applies. Scroll horizontally when the chart is wider than the page.

Bars are ordered by projected start. Changing the header project reloads the forecast immediately.

**Times** are in your local clock: the axis shows `HH:MM`, each row shows `start–end`, and the header shows when the queue is expected to drain (`~` marks an estimate that is not from the task's own history). The forecast is cached for about 20 s on the server and refreshed from the page at most once every 15 s after the previous request finished (slower after errors; a "stale" note appears if the last refresh failed). Very large queues show the first 300 bars. Prefer the header **project** filter when possible — scoped forecasts skip other projects' open runs and history. **Right-click** a bar for Details, Set priority, ±10, Stop, Retry, Force run, Disable and more; click a bar for the details drawer, which also has priority and enable/disable controls.
