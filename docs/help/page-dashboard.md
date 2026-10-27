---
id: page-dashboard
title: Dashboard
summary: Gauges, running cards, and short sparklines.
ui_pages: [dashboard]
audience: [human]
guide_anchors:
  web-ui: page-dashboard
---

The **Dashboard** shows CPU, GPU, RAM, queued, and daemon gauges. Each card has a time-series of recent samples (about the last 30 minutes in this browser tab). The line is scaled to that recent range, not to the machine total, so a small move in RAM or VRAM is visible. The caption under the line is the first value, the latest value, and how many minutes the line covers. Queued and running share one chart.

Running cards show **`#run_id`**. **Log** opens the bottom log panel rather than an inline tail. Status and metrics refresh via the shared SSE snapshot (same `/api/events` feed as Queue); polling is only a fallback when the stream is down.

- **FROZEN** badge: no recent stdout on a still-running process. That is not the same as blocking a project.
- **GPU badge**: the task requested VRAM (`resources.gpu_vram_gib` > 0).
