---
id: page-usage
title: Usage
summary: Historical utilization and cost trends.
ui_pages: [usage]
audience: [human]
guide_anchors:
  web-ui: page-usage
---

**Usage** charts resource history over 1d / 7d / 30d with line, area, or bar views. Filter by project to see overnight patterns.

Correlate with `tq report` and MCP `report` for aggregate error codes and run counts over the same window.

The summary table's **GPU h today (UTC)** column shows wall hours used since UTC midnight by tasks that declare `gpu_vram_gib`. When the project has a `max_gpu_hours_day` budget it reads `used / limit` and turns red once the budget is exhausted (the scheduler then holds back further GPU tasks; see `tq plan`).
