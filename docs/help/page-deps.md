---
id: page-deps
title: Deps tab
summary: Dependency graph visualization and pipeline repair.
ui_pages: [deps]
audience: [human]
guide_anchors:
  web-ui: page-deps
---

See the **Dependencies** topic below for graph modes and pipeline workflow. This tab is the primary place to unblock `depends_on` chains and retry downstream after fixing an upstream failure.

Pick which task statuses appear with the **status checkboxes**. Completed, disabled and cancelled start unchecked; unchecking a status removes those tasks (and their edges) from the graph even when something still depends on them. Each node shows its task name, status and project.

Export the graph with `GET /api/deps?format=dot` (Graphviz DOT; dead edges red, waiting edges dashed) or the default JSON; `project` and `focus` apply to both.

The graph is scaled to fill the pane **width** and scrolls vertically, with scrollbars when it is larger than the pane. The mouse wheel scrolls; **Ctrl+wheel** or pinch zooms (up to 20×). Drag pans, and **Reset view** fits the width again.
