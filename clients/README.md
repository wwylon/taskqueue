# Task clients — standard for all long jobs

Use these helpers in every YAML task script that reports progress, checkpoints, or honors cooperative control. Hand-printing `(N%)` is fine for a 10-second smoke test; anything longer should import `tq_client`.

Cookbook: [`docs/agents/task-script-cookbook.md`](../docs/agents/task-script-cookbook.md)  
Examples: [`examples/`](../examples/) · Dogfood: [`fixtures/tqdemo`](../fixtures/tqdemo/)

## Python

Single file, zero dependencies: [`python/tq_client.py`](python/tq_client.py).

Add `clients/python` to `PYTHONPATH`, or copy/symlink `tq_client.py` into the project.

```python
import tq_client as tq

tq.env_info(print_it=True)       # TQ_HOME / TQ_PROJECT / …
# tq.require_gpu()               # if resources.gpu_vram_gib > 0

cadence = tq.CheckpointCadence(every_n_units=1000)  # every_sec=600 default
for step in range(start, total):
    tq.maybe_suspend()           # suspend → exit 75; also honors hold
    tq.progress(100.0 * step / total, f"step {step}")
    tq.metric(step=step)
    if cadence.note_work():
        tq.save_checkpoint("ckpt.json", {"step": step})
        cadence.reset()
```

| Symbol | Purpose |
|--------|---------|
| `progress` / `metric` | `(N%)` and `TQ_METRIC` lines the daemon parses |
| `maybe_suspend` / `maybe_hold` | Cooperative control via `TQ_CONTROL_FILE` |
| `EXIT_SUSPENDED` (75) | Status `suspended`; pair with YAML `resume_args` |
| `CheckpointCadence` | Work units **or** ~10 min; `reset()` after save |
| `save_checkpoint` / `load_checkpoint` | Atomic JSON I/O |
| `progress_loop` | Iterable wrapper with rate-limited progress + suspend |
| `require_gpu` | Fail fast when CUDA / nvidia-smi is unavailable |
| `env_info` | Dump runner `TQ_*` identity vars |

Hold control JSON (from `tq hold` / MCP `hold_task`):

```json
{"action": "hold", "until": 1730563200, "duration_sec": 600, "reason": "cooling"}
```

## C++

Header-only: [`cpp/tq_client.hpp`](cpp/tq_client.hpp).

```cpp
#include "tq_client.hpp"

tq::progress(45.0, "epoch");
tq::maybe_suspend();
tq::CheckpointCadence cadence(/*every_n_units=*/1000);
if (cadence.note_work()) {
  tq::save_checkpoint("ckpt.json", "{\"step\":45}");
  cadence.reset();
}
```

Examples: [`cpp/example_progress.cpp`](cpp/example_progress.cpp), [`cpp/example_checkpoint.cpp`](cpp/example_checkpoint.cpp).

```text
g++ -std=c++17 -Iclients/cpp clients/cpp/example_checkpoint.cpp -o example_checkpoint
```

## Environment (set by the runner)

| Env | Meaning |
|-----|---------|
| `TQ_HOME` | State home |
| `TQ_PROJECT` | Project name |
| `TQ_TASK_ID` | Task id |
| `TQ_CONTROL_FILE` | Path to JSON control file |
| `TQ_DRY_RUN` | `1` when YAML `dry_run: true` |
| `TQ_OPERATOR` | Optional operator id |

Control file (written by `tq suspend`):

```json
{"action": "suspend"}
```

Exit **75** → task status `suspended` (resume via `resume_args` on retry).
