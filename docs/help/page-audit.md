---
id: page-audit
title: Audit
summary: Operator mutations, not a full access log.
ui_pages: [audit]
audience: [human]
guide_anchors:
  web-ui: page-audit
---

**Audit** lists operator actions: stop, retry, force, enable, priority, freeze, config, project limits, and alert or token changes. The actor is the `X-TQ-Operator` header when it is set, otherwise `web`. Heartbeats are not recorded.

**Export CSV** downloads the rows loaded on the page. This is not the daemon event log. A token secret is never stored in the detail.

**API:** `GET /api/audit` accepts `actor`, `action`, `project` (exact match), `since` / `until` (ISO timestamps, inclusive), `limit` (1–2000) and `before_id` (return events older than that id, for paging). `format=csv` or `format=jsonl` returns a server-side export of the filtered events instead of JSON.

**Config diff and rollback.** A `config` event records each changed key with its previous and new value (the `auth_token` is redacted). The Detail column shows the diff and a **Roll back** button, which restores the previous values and logs a `config_rollback` event. Keys that were unset before, no longer valid, or secret are skipped. Events recorded before this feature have no stored values and cannot be rolled back. **API:** `POST /api/audit/<id>/rollback` (admin scope).
