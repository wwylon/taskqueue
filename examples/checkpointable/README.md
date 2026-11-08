# checkpointable sample

Long-job pattern: `execution_semantics: checkpointable`, `auto_retry_on_cap`, `resume_args`, and a script that uses `tq_client`.

Register:

```yaml
projects:
  checkpointable:
    root: path/to/taskqueue/examples/checkpointable
    queue_dir: experiments/operator_queue
```

```powershell
tq sync
tq patch checkpointable/train-demo enabled=true
tq wait checkpointable/train-demo --timeout 120
```

The script path-inserts `../../clients/python` so it works from this tree without installing a package.
