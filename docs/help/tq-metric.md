---
id: tq-metric
title: Metrics (TQ_METRIC)
summary: Task-emitted key/value metrics shown as sparklines in the task drawer.
ui_pages: [queue, dashboard, history]
audience: [human, agent]
guide_anchors:
  web-ui: tq-metric
---

The **Metrics (TQ_METRIC)** block in the task drawer (Details → Info) is **not** a fixed list of daemon fields. It shows numeric keys your **task script** printed on stdout.

## How it works

Any stdout line of the form:

```text
TQ_METRIC {"epoch": 3, "loss": 0.124}
```

is parsed by the runner (and by the master for remote log chunks). Keys are **merged** into that run’s metric store — last value per key wins. Helpers:

- Python: `tq.metric(epoch=3, loss=0.124)` in `clients/python/tq_client.py`
- C++: `tq::metric({{"epoch", 3.0}, {"loss", 0.124}})` in `clients/cpp/tq_client.hpp`

## What you see in Details

- Only **numeric** keys get sparklines (last up to 60 samples per key; at most 8 keys tracked as series).
- The number next to each sparkline is the **latest** value for that key.
- Key **names are chosen by the task** (`loss`, `val_acc`, `etl_at_3`, …). Taskqueue does not define a standard schema for experiment metrics — ask the owning project what a given key means if it is unclear.

## Reserved keys (not task-settable)

| Key | Meaning |
|-----|---------|
| `_series` | Internal time series for sparklines (`[timestamp, value]` points). Tasks cannot set this via `TQ_METRIC`. |
| `log` | Auto-counted log issue chips: errors / warnings / other from stdout classification. Tasks cannot overwrite it. |
| `_progress` | Internal progress bookkeeping used by the runner. |

## Not the same as “📊 metrics”

The queue/dashboard **📊 metrics** button opens **resource usage** (CPU %, RAM GiB, VRAM GiB samples from the daemon). That is host sampling, not `TQ_METRIC` lines.

## Tips

- Prefer small, slowly changing numbers (loss, accuracy, ETA hours). Dumping huge tables into `TQ_METRIC` wastes disk in the run log and adds little to the sparkline.
- Progress percent still uses `(N%)` / `tq.progress` — that is separate from `TQ_METRIC`.
- Full reference: [task standards — Structured Metrics](/docs/task-standards).
