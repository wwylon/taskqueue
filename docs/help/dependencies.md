---
id: dependencies
title: Dependencies
summary: Deps graph modes, focus, pipelines, retry downstream.
ui_pages: [deps]
audience: [human, agent]
guide_anchors:
  web-ui: dependencies
---

Deps shows incomplete tasks and their dependency chains (**Mode: Active**). **Attention** = blocked/failed/stopped only. **All** = full graph. Dashed nodes are finished upstream kept for context.

Click a chain chip to isolate a component; focus selection + click drills into a neighborhood; **Retry downstream** resets a pipe. Pipelines: produce → optional check → consume via `depends_on` (AND). Fan-in with `depends_on_any` (OR) appears as edges with `kind: any`. Nodes may show `spawned_by` when a parent woke them via `on_complete` / `on_fail`.
