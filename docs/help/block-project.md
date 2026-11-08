---
id: block-project
title: Block project
summary: Stops new starts for one project; running tasks continue.
ui_pages: [queue, dashboard]
audience: [human, agent]
guide_anchors:
  web-ui: block-project
---

With a project selected, **Block project** (freeze) stops the scheduler from starting new tasks in that project only. Running tasks continue. Pause is a softer variant. Agents must not clear freeze/pause unless you ask.

This is **not** the same as per-task **Hold** (in-process idle via control file) or **Suspend** (exit 75). See [Stop, suspend, hold, freeze](cooperative-control.md).

This is different from the idle **FROZEN** badge on a running card (no recent log output).
