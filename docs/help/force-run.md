---
id: force-run
title: Force
summary: Bypass soft resource gates once; still waits on deps and locks.
ui_pages: [queue, dashboard]
audience: [human, agent]
guide_anchors:
  web-ui: force-run
---

Marks the task `force_run` and re-queues it so soft resource / anti-flap gates are bypassed once. You should see a toast immediately; the task still waits on unmet `depends_on`, named `resources.locks`, global `exclusive`, or per-project `exclusive_project`. On the Queue page, Force is only offered for `queued` tasks.
