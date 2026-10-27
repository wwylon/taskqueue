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
tq status
tq diagnose minimal/hello-progress
```
