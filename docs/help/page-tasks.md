---
id: page-tasks
title: Tasks
summary: Duration percentiles from completed runs.
ui_pages: [tasks]
audience: [human]
guide_anchors:
  web-ui: page-tasks
---

**Tasks** is one row per project and task from finished runs (`completed`, `failed`, `interrupted`, `suspended`, `stopped`). Duration is finish time minus start time.

**Average**, nearest-rank **P50**, and **P95** use **completed** runs only. Run count and success rate still cover all terminal statuses (success = share that completed). If a task has no completed samples, duration columns show `—`.

Columns are last finish, average, P50, P95, run count, and success rate. Search filters by project or task id. The header project filter applies. These are task rows, not a separate type catalog.
