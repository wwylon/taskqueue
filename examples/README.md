# Examples — taskqueue

Small copy-paste starters. For dogfood/CI fixtures (progress, freeze, over-ask, suspend), use [`fixtures/tqdemo`](../fixtures/tqdemo/).

## minimal

One local sleep task with `(N%)` progress:

1. Register the folder as a project (or copy `minimal/experiments/operator_queue/` into an existing project).
2. `tq sync` then `tq patch minimal/hello-progress enabled=true` (or edit YAML).
3. Watch with `tq status` / UI; inspect with `tq diagnose minimal/hello-progress`.

See [`minimal/README.md`](minimal/README.md).

## Reports & diagnose

```text
tq report --days 7
tq report --project minimal --days 3
tq diagnose minimal/hello-progress
```

HTTP: `GET /api/report?days=7`, `GET /api/diagnose?project=minimal&task_id=hello-progress`.
