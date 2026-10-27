# fixtures/tqdemo

Cheap dummy tasks for dogfooding and CI. No CUDA in the default set.

## Register

```powershell
cd path\to\taskqueue

# edit %USERPROFILE%\.taskqueue\projects.yaml — add:
# tqdemo:
#   root: path\to\taskqueue\fixtures\tqdemo

uv run tq sync
uv run tq list --project tqdemo
```

All YAMLs ship with `enabled: false`. Enable one at a time:

```powershell
# patch enabled: true in the YAML, or use the UI Enable control after sync
uv run tq sync
```

## Tasks

| task_id | Purpose |
|---------|---------|
| `dummy-progress` | Happy path `(N%)` |
| `dummy-resume` | Checkpoint + `resume_args` |
| `dummy-deps-a` / `dummy-deps-b` | `depends_on` chain |
| `dummy-pipe-a` → `dummy-pipe-check` → `dummy-pipe-b` | Canonical produce→check→consume pipe (`group: tqdemo/pipe`). Enable all three, then `tq group retry tqdemo tqdemo/pipe` or `tq retry tqdemo/dummy-pipe-a --downstream` to re-run the bunch. |
| `dummy-exclusive` | Exclusive gating |
| `dummy-exclusive-project` | Per-project exclusive gating |
| `dummy-frozen` | Hang after one progress line |
| `dummy-lie-progress` | Lie on `%` |
| `dummy-ask-cpu` / `dummy-ask-gpu` | Scheduler reject via over-ask |
| `dummy-fail` / `dummy-cap` | Fail + wall-clock cap |
| `dummy-suspend` | Poll control file → exit 75 (`tq suspend`) |
| `dummy-alloc-ram` | Optional capped alloc — **not** default CI |
| `test-stress` | Run `tests/test_stress.py` with progress lines (14 tests, ~100 s) |
| `test-scale` | Run `tests/test_scale.py` with progress lines (14 tests, ~1 s) |
| `stress-generate` | Insert 1 000 tasks across 40 fake projects into the live DB |
| `stress-biglog` | Stream ~120 MB of log output to stress the log viewer |
| `stress-cleanup` | Remove all `stress-*` tasks and run history from the live DB |

Exit code **75** → status `suspended`. See [`clients/README.md`](../../clients/README.md).

## Running the test tasks

Enable one in the UI or via patch, then sync:

```powershell
uv run tq sync --project tqdemo
# then enable via UI, or:
uv run tq patch tqdemo/test-stress enabled=true
```

`scripts/run_pytest.py` is the shared wrapper — it collects the test count, runs
pytest with `-v --tb=short`, and emits one `(N%)` progress line per test result so
the dashboard progress bar fills as tests pass. Works for any test file in the repo.

## Live UI stress workflow

```
stress-generate  →  browse web UI  →  stress-biglog  →  stress-cleanup
```

1. Enable + run `stress-generate` — inserts 1 000 tasks across 40 fake projects
   directly into the daemon DB. Open the queue, filter by project, check pagination.
2. Enable + run `stress-biglog` — streams ~120 MB in three phases (500k short lines,
   200 × 256 KB lines, 10k JSON blobs). Open the log viewer mid-run.
3. Enable + run `stress-cleanup` — removes every `stress-*` row. Priority 99 so it
   runs ahead of everything else.

All three are `enabled: false` by default. `stress-cleanup` is idempotent.
