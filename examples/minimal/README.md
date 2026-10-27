# minimal sample project

Register in `~/.taskqueue/projects.yaml`:

```yaml
projects:
  minimal:
    root: path/to/taskqueue/examples/minimal
    queue_dir: experiments/operator_queue
```

Then:

```powershell
tq sync
tq patch minimal/hello-progress enabled=true
tq wait minimal/hello-progress --timeout 60
tq result minimal/hello-progress
```

If it stays `waiting` / `tq.block.resources`, run `tq why minimal/hello-progress` (busy hosts often need `tq config set ram_limit_pct=95` or free RAM).
