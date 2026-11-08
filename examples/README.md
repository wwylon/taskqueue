# Examples — taskqueue

Copy-paste starters. **Prefer [`tq_client`](../clients/README.md)** for progress, checkpoints, and cooperative control — do not hand-print `(N%)` in new scripts.

Cookbook: [`docs/agents/task-script-cookbook.md`](../docs/agents/task-script-cookbook.md)  
Dogfood/CI fixtures: [`fixtures/tqdemo`](../fixtures/tqdemo/).

| Example | Shows |
|---------|--------|
| [minimal/](minimal/) | Smallest local progress task |
| [checkpointable/](checkpointable/) | `resume_args` + cadence + `auto_retry_on_cap` via `tq_client` |
| [pipeline/](pipeline/) | `depends_on` preprocess → train |
| [scheduled/](scheduled/) | `schedule.cron` nightly pattern |
| [snippets/](snippets/) | Matrix / GPU / ollama / remote YAML fragments |

## Reports & diagnose

```text
tq report --days 7
tq diagnose <project>/<task_id>
```
