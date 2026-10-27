# Progress Reporting Guide

Tasks report progress by printing lines matching `(N%)` to stdout — the daemon's log parser captures `progress_pct` live. This guide covers both the pattern and the client helpers.

## The pattern

Any stdout line containing `(N%)` or `(N.M%)` is treated as a progress update:

```
training epoch 3 (42.0%)
(67%)
```

The last matching line in each heartbeat window is stored as `progress_pct` on the run record and visible in the web UI progress bar.

## Python client (`tq_client.py`)

Copy `clients/python/tq_client.py` into your project (zero external dependencies).

```python
import tq_client as tq

# One-shot progress report
tq.progress(50.0, "halfway through training")

# Progress loop — reports pct + calls maybe_suspend() each iteration
batches = load_batches()
for batch in tq.progress_loop(batches, msg="training"):
    run_batch(batch)

# Manual suspend check
tq.maybe_suspend()

# Checkpoint cadence: due when work units OR ~10 min; reset both after save
cadence = tq.CheckpointCadence(every_n_units=1000)  # every_sec=600 default
if cadence.note_work():
    tq.save_checkpoint("ckpt.json", {"epoch": epoch, "loss": loss})
    cadence.reset()
# long single unit (one epoch >> 10 min): if cadence.due(): save; cadence.reset()
# suspend / SIGTERM: always save, then cadence.reset()
state = tq.load_checkpoint("ckpt.json") or {}
epoch = state.get("epoch", 0)
```

`progress_loop` wraps any iterable and:
1. Calls `maybe_suspend()` (cooperative pause) and prints `(pct%)` on the first item, then at most once per `min_interval_sec` (default 1 s), so tight loops do not read the control file per item.
2. Prints `(100%)` when the loop completes.
3. Takes the length from `total=` or `len(iterable)`; a plain iterator is not copied into a list, and without a known length no percent lines are printed.

The C++ `tq::progress` prints fixed-point percentages (`(0.00%)`); `progress_range` refuses temporary containers at compile time because it keeps a reference.

## C++ client (`tq_client.hpp`)

Copy `clients/cpp/tq_client.hpp` into your project (header-only, C++17+).

```cpp
#include "tq_client.hpp"

// One-shot progress
tq::progress(50.0, "halfway");

// Progress range over a container
std::vector<Batch> batches = load_batches();
for (auto& batch : tq::progress_range(batches, "training")) {
    run_batch(batch);
}

// Manual suspend check
tq::maybe_suspend();

// Checkpoint cadence (file I/O is caller-owned)
tq::CheckpointCadence cadence(/*every_n_units=*/1000);
if (cadence.note_work()) { /* write checkpoint */ cadence.reset(); }
```

## Cooperative suspend

The daemon can request a task to suspend (pause and save a checkpoint) by writing to the control file (`TQ_CONTROL_FILE` env var). When you call `tq.maybe_suspend()` / `tq::maybe_suspend()`, the task reads this file and exits with code 75 (`EXIT_SUSPENDED`) if a suspend was requested. The daemon re-queues the task with `resume_args` so it can restart from the checkpoint.

Cooperative suspend is only effective when:
- The task polls `maybe_suspend()` regularly (each loop iteration, or at least every few seconds).
- `resume_args` is set in the task YAML.
- The task writes a checkpoint before exiting 75.

## `stopped_by` codes

When the scheduler stops or interrupts a run, `stopped_by` records the reason:

| Code | Meaning |
|------|---------|
| `tq` | Manual `tq stop` or API stop |
| `host-sleep` | Host went to sleep (daemon detected clock jump) |
| `lease-timeout` | Worker lease expired (worker crashed or network issue) |
| `wall-cap` | `wall_clock_cap_sec` exceeded |
| `tq.stop.ram` | Host RAM exceeded soft gate; auto-restartable |
| `tq.infra.hung` | No stdout progress for `hung_task_k` ticks (idempotent tasks only) |
