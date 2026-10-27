# Security Guide

## Overview

`taskqueue` is designed for a **trusted LAN / single-host** environment. It is not a hardened web service. This guide documents the security properties honestly so you can make informed decisions about how to deploy it.

## Auth token

By default the daemon binds to `127.0.0.1` (loopback only) with no auth token. Any process on the same machine can reach the API.

Setting an auth token is **required** before exposing the daemon on a network interface:

```bash
tq token-generate          # generates and stores a random token
tq config set auth_token=$(tq token-generate --print)   # manual
```

Once set, all `/api/*` requests must include the token in one of:
- `Authorization: Bearer <token>` header
- `X-TQ-Token: <token>` header
- `?tq_token=<token>` query parameter (convenient for browser use, avoid logging URLs). The web UI stores it in `localStorage` on first load and strips it from the address bar.
- `?tq_ticket=<ticket>` on the streaming endpoints only (`/api/events`, `/api/logs/<id>/stream`): the web UI POSTs `/api/sse-ticket` (normal header auth) and opens `EventSource` with the returned 120 s ticket, so the long-lived token stays out of URLs, logs and Referer headers. A ticket inherits the issuer's scope and project limits, works for GET streams only, and is held in daemon memory (lost on restart).

**Token is stored in plaintext** in `~/.taskqueue/queue.db` (SQLite `config` table). This is intentional — the goal is authentication, not secret management. If you need stronger protection, restrict filesystem access to the queue home directory.

## Non-loopback bind

To expose the daemon on the LAN (e.g. for remote workers):

```bash
tq config set auth_token=<strong-random-token>
# Then start daemon with bind flag (not yet a CLI option — set in systemd unit):
# python -m taskqueue.daemon --bind 0.0.0.0 --port 8765
```

The daemon **refuses** to bind to a non-loopback address without `auth_token` being set.

## CSRF protection

State-changing methods (`POST`, `PUT`, `PATCH`, `DELETE`) and `GET /api/worker/next-task` reject a foreign `Origin` or `Sec-Fetch-Site: cross-site` with 403. The check runs whether or not `auth_token` is set. Any `/api` request (including `GET`) with `Sec-Fetch-Site: cross-site` is rejected. Same-origin browser requests usually omit `Origin` and are allowed. The API sends **no** `Access-Control-Allow-Origin` header: the web UI is same-origin, so other web pages cannot read API responses.

## Browser hardening headers

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: same-origin`. The UI page (`/`) also sends a Content-Security-Policy: scripts only from this origin (no inline scripts; the theme bootstrap is `/static/theme-boot.js`), connections only to this origin, and no framing. Inline styles are still allowed.

## Worker authentication

Remote workers authenticate with `config.auth_token` only. A scoped API token cannot register, heartbeat, claim work, or take the master lease. Treat workers as trusted clients — a worker can finalize any run it holds a lease for, and can read task YAML including environment variables passed via `env:`.

## Scoped API tokens

The web **API** page (and `POST /api/tokens`) creates operator tokens for the HTTP API. The secret is shown once (`tq_live_` plus random bytes). The database stores only its SHA-256 hash, a short prefix, and a scope:

| Scope | Allows |
|-------|--------|
| `read` | `GET` (secret-stripped task/run DTOs — no `env`, `args_override`, `finalize_token`, or absolute `log_path`) |
| `write` | `GET` plus task mutations (stop, retry, force, enable, priority, freeze) |
| `admin` | Everything, including task YAML read/save (`GET`/`PUT /api/tasks/.../yaml` — YAML may contain `env:`), config, project limits, alert rules, and token create/revoke |

**Operator read vs workers.** Scoped `read`/`write` tokens never receive capability secrets (`finalize_token`, `worker_session_id`) or task `env` on JSON APIs. Raw YAML (`GET …/yaml`) requires `admin`. Remote workers still receive full task YAML/`env` by design (trusted clients).

**Transfer / data capability tokens.** Workers send `X-TQ-Finalize-Token` (preferred) with `run_id` in the query; query `finalize_token=` remains accepted for compatibility. Transfer and CAS routes require the master lease (same as claim/finalize). Notification APIs redact webhook URL userinfo/query before returning dead letters or notify.jsonl payloads.

**SSE and webhooks.** Concurrent SSE streams are capped at a quarter of the HTTP worker pool so streams cannot starve REST calls; idle streams send a `: ka` comment every 15 s so dead sockets free their slot. Webhook delivery uses a no-redirect opener: a redirect to a non-loopback host is refused before the next request is sent. `POST /api/stop` and `/api/mark-processed` return 404 for unknown tasks; stop-all and mark-processed are audited.

**Project-limited tokens.** `POST /api/tokens` accepts an optional `projects` list (not allowed with `admin`). Such a token is default-deny: it may only `GET` `/api/queue`, `/api/history` and `/api/audit`, and every `project` query value must be in its list (a request without `project` is refused). All other routes - dashboard, status, tasks, workers, config, SSE, metrics and every write - return 403. An unparseable stored list allows nothing. Loopback clients skip token checks entirely, so scoping only constrains remote callers.

`config.auth_token` still counts as **admin**. Loopback clients (`127.0.0.1`, `::1`) skip the token check, so the local UI works without storing a token — but only when the request is direct: its `Host` header must be a local name (`localhost`, `*.localhost`, an IP literal or this machine's hostname) and it must carry no `X-Forwarded-For` / `Forwarded` / `X-Real-IP` header. A reverse proxy on the same machine or a DNS-rebinding page therefore has to present the token. `POST /api/cli/add` writes the submitted YAML under a unique file name by default and validates it before it reaches the queue directory. Revoke sets `revoked_at`; it does not delete the row. Operator audit `detail` never contains the secret.

```bash
curl -H "Authorization: Bearer tq_live_…" http://127.0.0.1:8765/api/status
```

Machine-readable route list: `GET /api/docs`.

## Operator audit

Mutating operator actions are appended to `operator_audit`: stop, retry, force, enable, priority, freeze, config, project limits, alert/token changes, and health acknowledgements (`health_ack`). The actor is the `X-TQ-Operator` header when present, otherwise `web`. Heartbeats are not recorded. The Audit page can export the loaded rows as CSV. This is not a full HTTP access log and it is not `daemon_events`.

## Prometheus metrics

`GET /api/metrics` returns Prometheus text (version 0.0.4): `tq_tasks{status}`, `tq_running_tasks{project}`, `tq_workers{state}` and `tq_daemon_rss_megabytes`. It lives under `/api`, so the normal token and scope checks apply: a `read`-scope token is enough and loopback clients need no token. Scrape config:

```yaml
scrape_configs:
  - job_name: taskqueue
    metrics_path: /api/metrics
    authorization: {credentials: "<read-scope token>"}
    static_configs: [{targets: ["host:8765"]}]
```

## Task provenance

Since schema 22 each task row records how it was created: `submitted_via` (`cli` | `mcp` | `web` | `api` | `yaml`), `submitted_by` (operator name) and `client_info` (e.g. `mcp/claude-code 2.0`). They come from the `X-TQ-Via`, `X-TQ-Operator` and `X-TQ-Client` request headers that `tq` and `tq mcp` send (operator = `TQ_OPERATOR`). The values are **self-reported analytics, not authentication**. They are set once at creation and never overwritten; tasks that predate schema 22 have `NULL`. `POST /api/cli/add` is also written to the operator audit as `task_add`. Remote workers on an older schema are drained on the next heartbeat, as for any schema bump.

## Usage analytics

Schema 23 adds `api_usage`: **hourly aggregate counters** of API/MCP calls, keyed by `channel` (`mcp` | `cli` | `api` | `ui` | `worker` | `http`), `client`, `operator`, `project`, `op` and `error_code`, with `calls`, `total_ms` and `max_ms`. `op` is always an MCP tool name or an API route *template* (`POST /api/stop/<project>/<task_id>`), never a raw path.

- **Not stored:** arguments, request/response bodies, YAML, log text, tokens. No per-request events.
- **Identity is self-reported** (`X-TQ-Operator` = `TQ_OPERATOR`, `X-TQ-Client` = e.g. `mcp/claude-code 2.0`); use it for analytics only.
- Each interface counts its own top-level calls once: the MCP process counts tool calls, the `tq` CLI counts every invocation (`op` = `tq <command>`, including DB-direct reads), and the HTTP requests they trigger (`X-TQ-Via: mcp|cli`) are not counted again. The daemon counts `ui` (browser), `worker`, `api` and other `http` callers.
- Read it back with `tq agents`, `GET /api/agent-usage` or MCP `agent_usage_summary`.
- Failures are recorded as `error_code` (`http.<status>`, `mcp.error`, a tool's own `error_code`, CLI `exit.<rc>`) and counted in `errors`.
- **Wait outcomes are not errors (and not notice latency):** `wait.timeout` (MCP `wait_for_task` timed out; `tq wait` exit 2, or 124 with `--raw-exit`) and `wait.task_failed` (`tq wait` exit 1 — a waited task failed/stopped; any other nonzero with `--raw-exit`) stay visible as `error_code` but are excluded from `errors` **and** from `avg_ms` / `max_ms`. `tq wait` exit 3 (bad args / not found) is still `exit.3`. Group by `error_code` to see wait rows themselves.
- `tq wait` sets `project` from its `project/task_id` keys, `--project` and `--downstream project/root`: the one project they all name, `(multi)` if several, empty if none (e.g. only `--suite` / `--mine`).
- HTTP counters are buffered in memory and flushed at most once a minute (and hourly by the daemon); a crash can lose up to a minute.
- **Retention:** `retention_usage_days` (default 400) pruned hourly by the daemon. **Disable:** `tq config set usage_tracking=0`.

## notify_url webhooks

Webhook URLs default to **loopback-only** (`127.0.0.1`, `localhost`, `::1`). This prevents a maliciously crafted YAML from causing the master to POST to arbitrary external services.

To allow remote webhook targets:

```bash
tq config set notify_allow_remote=1
```

Failed webhook deliveries are retried 3 times (1s, 4s, 9s delays) then written to `~/.taskqueue/notify_failed.jsonl` for manual inspection.

## What this system does NOT provide

- **No TLS** — all traffic is plaintext HTTP. Use a reverse proxy (nginx, Caddy) with TLS if you need encryption on the wire.
- **No multi-user isolation** — all operators share one DB; ownership is self-reported via `TQ_OPERATOR` or `X-TQ-Operator`.
- **No sandboxing** — tasks run with full user permissions. A malicious task can read any file the user can read.
- **No HTTP access log** (aggregate counters only, see Usage analytics) — operator mutations are in `operator_audit` (see above). `notify.jsonl` records task notifications, not every API call.

## Recommended deployment

For a single-host development machine:
- Keep the default loopback bind.
- No auth token needed.
- The OS user-boundary is your security perimeter.

For a LAN with remote workers:
- Set a strong `auth_token` (e.g. `tq token-generate`).
- Bind to the host's LAN address, not `0.0.0.0` if possible.
- Consider a reverse proxy with TLS if the LAN is not fully trusted.
- Treat workers as trusted; use `requires: {os: linux}` to restrict which workers can claim tasks, not for security.

## Data retention

Retention is opt-in (`retention_enabled`, default off). When enabled, the daemon runs a pass
hourly (and `tq prune --apply` runs the same logic by hand):

- run records older than `retention_run_days` (default 180) are deleted together with their
  `resource_history` rows, in batches of 500; the run a task points at (`last_run_id`) is kept;
- log files older than `retention_log_days` (default 90) are deleted, except logs of unfinished
  runs and each task's latest run;
- after age deletion, finished log files larger than `retention_log_max_bytes` (default 1 MiB when
  unset; set `0` to disable) are rewritten to keep only that many trailing bytes (newline-aligned).
  Latest-run logs are capped too; unfinished runs are skipped. **mtime is preserved** so capping
  does not delay age deletion. Truncation is lossy (early stdout is dropped);
- `operator_audit` rows older than `retention_audit_days` (default 365) are deleted;
- when rows were removed the WAL is checkpointed (`TRUNCATE`) and `incremental_vacuum` is run.

Independent of `retention_enabled`, the hourly pass rotates `notify.jsonl`, `notify_failed.jsonl`
and `prune_log.jsonl` to `<name>.1` above 10 MB, and `tq start` rotates `daemon.log` to
`daemon.log.1` above 20 MB. While running, the daemon checks every minute and copies `daemon.log` to
`daemon.log.1` and truncates it above 50 MB. Per-run heartbeat lines (`>> project/task | elapsed=...`) are
written when progress changes, when a run is frozen or suspect, and otherwise every 5 minutes.

`retention_projects` takes JSON, e.g. `{"myproj": {"run_days": 30, "log_days": 14}}`.

## Webhook dead letters

A `notify_url` webhook is attempted three times (1 s, 4 s, 9 s back-off). When all attempts fail
the event is stored in the `notify_dead` table (and still appended to `notify_failed.jsonl`).
`GET /api/notify/dead` lists them; `POST /api/notify/dead/<id>/retry` replays one with a single
attempt (the URL is re-validated against `notify_allow_remote`) and deletes it on success.
This feature bumps the database schema to 26: upgrade workers together with the master.

## Worker credentials

Once a worker has registered with a `credential_hash` (SHA-256 of its token), the master enforces it: re-registering, deregistering and rotating all require the matching hash (`old_credential_hash` for rotation), compared in constant time. A mismatch or missing hash returns 403. Use `tq worker rotate-credential` to change it.
