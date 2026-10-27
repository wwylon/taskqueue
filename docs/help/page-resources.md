---
id: page-resources
title: Resources
summary: Live cluster and per-task resource usage.
ui_pages: [resources]
audience: [human]
guide_anchors:
  web-ui: page-resources
---

**Resources** shows current cluster CPU/RAM/GPU usage and per-running-task sparklines. Ollama sidecar VRAM may appear when tasks hold the `ollama` lock.

Use this tab when diagnosing `tq.block.resources` or planning whether to force-run under pressure.
