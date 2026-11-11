# scheduled sample

Nightly (or any cron) pattern using YAML `schedule.cron` (UTC). After a terminal status the daemon re-queues on match — no Windows Task Scheduler required.

```yaml
projects:
  scheduled:
    root: path/to/tqdaemon/examples/scheduled
    queue_dir: experiments/operator_queue
```

Enable once (`tq patch scheduled/nightly-ci enabled=true`), then leave it. Keep `enabled: true` for cron to fire after terminal runs.
