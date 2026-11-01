# Queue hygiene

Use hygiene when the queue grows (many completed tasks, heavy projects) and you
want rates and archive suggestions — not when you need live status.

## What it reports

- **Churn:** how many **new task rows** were added (`enqueued_at` — not YAML
  mtime), how many runs started, and how many were **re-runs**.
- **Multi-use:** tasks completed many times over days (reusable pipelines). Keep
  these; do not archive them by default.
- **Flaky:** many attempts with few completions — review manually.
- **Archive proposals:** projects with stale one-shot completed tasks that
  `tq archive … --max-completed-runs 1 --dry-run` would take. Shared multi-task
  YAMLs are skipped (same rule as archive).

## Commands

```text
tq hygiene --json
tq report --hygiene --days 7 --older-than 30d
```

MCP: `report` with `kind=hygiene`. HTTP: `GET /api/hygiene`.

## Safety

Hygiene **never** archives. Proposals include a dry-run command. Drop
`--dry-run` only after an operator (or an agent with an explicit apply ask)
approves.

Related: `tq prune` deletes old **runs/logs** (retention); `tq archive` removes
**task rows** from the live queue into `<home>/archive/`.
