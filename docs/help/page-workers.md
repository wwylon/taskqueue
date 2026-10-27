---
id: page-workers
title: Workers
summary: Remote worker registry, health, and dispatch.
ui_pages: [workers]
audience: [human]
guide_anchors:
  web-ui: page-workers
---

**Workers** lists registered remote machines. Status reflects heartbeat freshness and lease health. Tasks with `dispatch: remote` prefer online workers; local fallback applies when none match.

Use this tab to confirm a worker is online before debugging remote-only tasks. See [WORKER_SETUP.md](/docs/task-standards) in the repo for configuration.
