# Task clients

Helpers for YAML task scripts talking to the queue (progress, checkpoints, cooperative control).

## Python

```python
from tq_client import (
    CheckpointCadence,
    EXIT_SUSPENDED,
    poll_control,
    progress,
    save_checkpoint,
)

cadence = CheckpointCadence(every_n_units=1000)  # every_sec=600 default

# In your loop:
progress(pct)
if cadence.note_work():
    save_checkpoint(...)
    cadence.reset()
action = poll_control()
if action == "suspend":
    save_checkpoint(...)
    cadence.reset()
    raise SystemExit(EXIT_SUSPENDED)
```

Add `clients/python` to `PYTHONPATH`, or copy/symlink `tq_client.py` into the project.

## C++

Header-only: [`cpp/tq_client.hpp`](cpp/tq_client.hpp).

```cpp
#include "tq_client.hpp"

tq::progress(45.0, "epoch");
tq::maybe_suspend();  // exits 75 if TQ_CONTROL_FILE asks for suspend

tq::CheckpointCadence cadence(/*every_n_units=*/1000);
if (cadence.note_work()) { /* write checkpoint */ cadence.reset(); }
```

Example: [`cpp/example_progress.cpp`](cpp/example_progress.cpp).

```text
g++ -std=c++17 example_progress.cpp -o example_progress
# or: cl /EHsc /std:c++17 example_progress.cpp
```

## Environment (set by the runner)

| Env | Meaning |
|-----|---------|
| `TQ_HOME` | State home |
| `TQ_PROJECT` | Project name |
| `TQ_TASK_ID` | Task id |
| `TQ_CONTROL_FILE` | Path to JSON control file |
| `TQ_OPERATOR` | Optional operator id |

Control file (written by `tq suspend`):

```json
{"action": "suspend"}
```

Exit **75** → task status `suspended` (resume via `resume_args` on retry).
