# Task script cookbook

**Standard for every long job:** use [`clients/`](../../clients/README.md) (`tq_client` / `tq_client.hpp`), not hand-printed `(N%)` loops. YAML contracts live in [task_standards.md](../task_standards.md); this page is the copy-paste pattern.

## Minimum viable long job

### YAML checklist

```yaml
task_id: train_v1
command: python
args: [scripts/train.py, --out, runs/train_v1]
resume_args: [scripts/train.py, --out, runs/train_v1, --resume]
wall_clock_cap_sec: 86400
execution_semantics: checkpointable
auto_retry_on_cap: true
resources:
  cpu_cores: 4
  ram_gib: 16.0
  # gpu_vram_gib: 5.4   # if set, pass --require-gpu and call tq.require_gpu()
```

Copy-paste project: [`examples/checkpointable/`](../../examples/checkpointable/).

### Python loop

```python
import sys
from pathlib import Path

# Prefer: PYTHONPATH=…/tqdaemon/clients/python  or symlink tq_client.py into the project
sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "clients" / "python"))
import tq_client as tq

ckpt = Path("runs/train_v1/checkpoint.json")
start = 0
if "--resume" in sys.argv:
    state = tq.load_checkpoint(ckpt) or {}
    start = int(state.get("step", 0))

cadence = tq.CheckpointCadence(every_n_units=100)  # or every ~10 min
for step in range(start, 1000):
    tq.maybe_suspend()  # also honors hold
    # … do work …
    tq.progress(100.0 * (step + 1) / 1000, f"step {step}")
    tq.metric(step=step)
    if cadence.note_work():
        tq.save_checkpoint(ckpt, {"step": step + 1})
        cadence.reset()
```

### C++ twin

```cpp
#include "tq_client.hpp"
// load prior JSON with tq::load_checkpoint; write with tq::save_checkpoint
tq::CheckpointCadence cadence(/*every_n_units=*/100);
tq::progress(45.0, "epoch");
tq::maybe_suspend();
if (cadence.note_work()) {
  tq::save_checkpoint("runs/train_v1/checkpoint.json", "{\"step\":46}");
  cadence.reset();
}
```

## Other starters

| Need | Where |
|------|--------|
| Tiny progress-only task | [`examples/minimal/`](../../examples/minimal/) |
| `depends_on` pipeline | [`examples/pipeline/`](../../examples/pipeline/) |
| Nightly cron | [`examples/scheduled/`](../../examples/scheduled/) |
| Matrix / GPU / ollama / remote fragments | [`examples/snippets/`](../../examples/snippets/) |
| Dogfood suite | [`fixtures/tqdemo`](../../fixtures/tqdemo/README.md) |

## Runner environment

| Env | Set by | Meaning |
|-----|--------|---------|
| `TQ_HOME` | runner | State home |
| `TQ_PROJECT` | runner | Project name |
| `TQ_TASK_ID` | runner | Task id |
| `TQ_CONTROL_FILE` | runner | Cooperative control JSON path |
| `TQ_DRY_RUN` | runner when `dry_run: true` | Scripts should no-op side effects |
| `TQ_OPERATOR` | optional operator env | Attribution (not usually set by scripts) |
| `TQ_WORKSPACE` / `TQ_PACKAGE_HASH` | remote worker | Per-attempt work root / package |

`tq.env_info(print_it=True)` dumps the common keys.

## GPU

When `resources.gpu_vram_gib > 0`, pass `--require-gpu` (or equivalent) in `args` / `resume_args` and call `tq.require_gpu()` at startup so a missing CUDA stack fails clearly instead of silently falling back to CPU. See task_standards §3.
