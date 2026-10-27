---
id: page-config
title: Config
summary: Daemon limits, frozen threshold, per-project caps.
ui_pages: [config]
audience: [human]
guide_anchors:
  web-ui: page-config
---

**Config** edits live daemon settings: CPU/GPU/RAM soft limits, `max_running_tasks`, graceful stop seconds, and **frozen threshold** (idle detection).

Per-project rows set max concurrent tasks, **min tasks** (slots held back from other projects while this project has queued work and fewer running; only meaningful with a global `max_running_tasks`; `force` bypasses), and RAM/VRAM caps. Changes apply on save; running tasks are not restarted automatically.

See [scheduler.md](/docs/scheduler) for how admission uses these limits.
