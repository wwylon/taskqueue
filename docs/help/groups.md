---
id: groups
title: Groups
summary: YAML group field for bulk ops and queue filter chips.
ui_pages: [queue, groups]
audience: [human, agent]
guide_anchors:
  web-ui: groups
---

YAML `group`: labels tasks for bulk stop/retry and for the group filter chips under status/project on the Queue page. Groups do not change admission priority.

Different from `suite` (enable/disable all members) — `group` is for operational batch actions in the UI and CLI.
