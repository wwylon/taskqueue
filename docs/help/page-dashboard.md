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

Under the gauges, **Fits now** shows the CPU cores, RAM and VRAM still free after running tasks, and which queued task would start next — or, if nothing fits, the task at the head of the queue and why it is blocked. It comes from the same dry run as `tq plan` (declared resources; estimates, mutexes, services and the P6 hold are not simulated), refreshes when a task starts or finishes and otherwise at most every 15 s, and only while the Dashboard is visible.

Running cards show **`#run_id`**. **Log** opens the bottom log panel rather than an inline tail. In that panel, lines keep the colours the task printed; the level filter and **First error** use the same rules as the run's warn/error counts, and new output scrolls into view only while you are at the bottom (scroll up to read without being pulled down). Status and metrics refresh via the shared SSE snapshot (same `/api/events` feed as Queue); polling is only a fallback when the stream is down.

- **FROZEN** badge: no recent stdout on a still-running process. That is not the same as blocking a project.
- **GPU badge**: the task requested VRAM (`resources.gpu_vram_gib` > 0).
