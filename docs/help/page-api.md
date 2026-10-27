---
id: page-api
title: API tokens
summary: Scoped HTTP tokens. Workers keep auth_token.
ui_pages: [api]
audience: [human]
guide_anchors:
  web-ui: page-api
---

**API** creates a scoped token. The secret (`tq_live_…`) is shown once. The database stores a SHA-256 hash and a prefix.

- **read** — GET
- **write** — GET plus task changes (stop, retry, force, enable, priority, freeze, YAML; many tasks at once via `POST /api/tasks/bulk`)
- **admin** — config, project limits, alerts, and token create or revoke, plus write

The optional **Projects** field (comma separated) limits a read or write token to those projects. Such a token may only read the queue, history and audit lists with a `project` filter naming an allowed project; every other route returns 403. It cannot be combined with admin. See [security](../security.md).

The daemon `auth_token` remains an admin credential. Workers and the master lease keep using that token, not a scoped one. **Revoke** marks the token revoked. Loopback browsers skip the token check.

```bash
curl -H "Authorization: Bearer tq_live_…" http://127.0.0.1:8765/api/status
```

Route list: [/api/docs](/api/docs).
