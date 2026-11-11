# pipeline sample

Two tasks with `depends_on`: preprocess → train. Both scripts use `tq_client.progress`.

```yaml
projects:
  pipeline:
    root: path/to/tqdaemon/examples/pipeline
    queue_dir: experiments/operator_queue
```

```powershell
tq sync
tq patch pipeline/preprocess enabled=true
tq patch pipeline/train enabled=true
tq wait pipeline/train --timeout 120
```
