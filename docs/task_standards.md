# Task Standards and Conventions for `taskqueue`

This document defines the requirements, standards, and best practices that tasks running in the `taskqueue` system must follow. Designing tasks to adhere to these rules ensures they can be scheduled fairly, monitored accurately, and restarted cleanly in our multi-project research environment.

Prefer YAML + `tq sync` for long jobs; keep short interactive work in-session. See [README](../README.md) (session work vs the queue) and [agents/recipes.md](agents/recipes.md).

### Referring to runs by `#`

Every execution attempt gets a numeric `run_id`. The Web UI shows it as **`#<run_id>`** on the dashboard (running cards) and in history. When an agent or user talks about a specific attempt — stop, logs, metrics, failure — prefer citing that number (e.g. `#142`) together with `project/task_id`. The same `#` appears in MCP/`tq` payloads as `run_id`.

### Runtime identity (dogfood)

* Default state home is `~/.taskqueue` (port **8765**). Confirm with `tq doctor`.
* Run `tq doctor` before overnight leave — it prints home, schema version, pid, port, and `protocol` (`PROTOCOL_VERSION` in config).
* Cheap validation tasks live under [`fixtures/tqdemo`](../fixtures/tqdemo/README.md); do not point experimental workers at the prod master.

---

## 0. YAML field reference

Fields recognized by `parse_task_yaml` (see `taskqueue/db.py`). Required fields are marked.

| Field | Default | Notes |
|-------|---------|-------|
| `task_id` (or `id`) | **required** | No `/`, `\`, `..` or control characters in the id; not a reserved Windows name. Project comes from which queue dir the YAML lives in. |
| `command` | **required** | Must be non-empty. Bare name (`python`, `wsl`) or project-relative path. Prefer `python` for portability. |
| `args` | `[]` | Argv after the command. |
| `description` | `""` | Human-readable summary. |
| `resume_args` | unset | Used on retry after interrupt; mandatory when the tool supports `--resume`. |
| `wall_clock_cap_sec` | unset | Mandatory for hang-prone jobs. Pilots ~3600; full runs ~86400. |
| `resources.cpu_cores` | `1` | Logical cores. |
| `resources.ram_gib` | `0.0` | Requested RAM GiB. |
| `resources.gpu_vram_gib` | `0.0` | Requested VRAM GiB admission guard. **Not a device flush guarantee** — see §3 GPU note. Tasks with `gpu_vram_gib > 0` get a GPU badge in the UI / `requires_gpu` in `tq list --json`. |
| `resources.locks` | `[]` | Named mutexes. Two running tasks may not share a lock (`tq.block.mutex`). Also used to attribute sidecar VRAM (e.g. `locks: [ollama]` matches `ollama.exe` and Windows `llama-server.exe`). Ignore generic names (`python`, `node`, `java`). |
| `on_dependency_failure` | `block` | `block` or `cancel` — what happens to this task when a dependency fails. See *Dependency Management*. |
| `negative_exit_codes` | `[]` | Exit codes that mean "ran fine, the answer is no" (e.g. a gate whose rule was not met). Such a run is `completed` with code `tq.run.negative`, not `failed`; `0` is not allowed. |
| `on_dependency_negative` | `run` | `run` or `cancel` — what this task does when a `depends_on` peer completed with a negative result. |
| `class` | `normal` | `normal` or `scavenger`. A **scavenger** is low-value filler: it is admitted only when no non-scavenger task is waiting on capacity (`tq.block.scavenger` otherwise), and is cooperatively suspended (control file, then `suspend_grace_sec`) when other work becomes capacity-blocked → status `stopped` / `tq.stop.scavenger`, auto-restarted with `resume_args` when capacity is idle again. Local daemon only (v1); incompatible with `execution_semantics: at_most_once`. See `docs/scheduler.md`. |
| `priority` | `50` | Higher number = sooner. Live change: `tq priority project/id N`. |
| `enabled` | `true` | When `false`, task status becomes `disabled` (for non-terminal rows) until re-enabled. **`enabled: true` does not re-run a terminal task** (`completed` / `failed` / …) — use `tq retry` (or `tq reverify`). `tq list --json` exposes DB `enabled` after the last sync/patch. |
| `schedule` | unset | Optional `{cron: "MIN HOUR DOM MON DOW"}`. After a terminal status (`completed`, `failed`, `interrupted`, `suspended`, `stopped`), the daemon re-queues the task when a minute in the expression matches (UTC). The web **Schedule** page lists these rows and the next fire within 48 hours; it does not create a task. Pause sets `enabled: false`. |
| `exclusive` | `false` | When true, no other task may run while this one runs (and it waits if anything is already running). Prefer full `gpu_vram_gib` over blanket `exclusive` when CPU tasks should continue — see §3 GPU note. |
| `exclusive_project` | `false` | When true, no other task from the **same project** may run while this one runs (and it waits if any same-project task is already running). Tasks from other projects are unaffected. Weaker than `exclusive`; stronger than relying only on per-project resource limits. |
| `depends_on` | `[]` | AND: same-project `task_id`s (or `project/task_id`); blocked until **each** completes successfully. |
| `depends_on_any` | `[]` | OR: blocked until **any one** peer completes successfully. May combine with `depends_on` (both gates apply). |
| `requires_paths` | `[]` | Paths that must exist (resolved vs project root) before start. |
| `dispatch` | `local` | `local` \| `remote` \| `any` — see §4. |
| `requires` | `{}` | Worker constraints: `os`, `arch`, `min_ram_gib`, … — see §4. |
| `bundle` / `bundle_hash` | unset | Pin a worker artifact: `bundle: { hash: "<sha256>" }` or top-level `bundle_hash`. Built with `tq bundle <project>`. Remote workers **do not unpack onto `project.root`** — they copy into a per-attempt workspace under `{home}/runs/<run_id>/work`. |
| `code.provider` | auto | Remote code packaging (P5 / R-009). `git_patch` (default when the project is a git repo): master captures `HEAD` + a content-addressed overlay of dirty/untracked files; worker materializes `{home}/cache/code/<package_hash>/` then a writable per-attempt copy. `bundle` keeps the existing tarball provider. `project_root` runs from the worker checkout (legacy). Explicit `code: {provider: …}` wins. |
| `ledger_subdir` | unset | Opaque passthrough for project experiment ledgers; ignored by the scheduler. |
| `max_attempts` | `0` | Max failed runs before the scheduler stops admitting; `0` = unlimited. Once exhausted, the task is marked **`failed`** with `block_reason` / message like `max_attempts=N reached` (F-030). **`tq retry` alone will not unstick it** while the failed-run counter still meets the ceiling — raise or clear the budget first: `tq patch <project/task_id> max_attempts=0` (or a higher number), then `tq retry`. Surfaced via `tq diagnose` / MCP `attention_list`. |
| `notify_url` | unset | Opt-in end-of-task notify. When set, after a successful terminal finalize (`completed` / `failed` / `interrupted` / `suspended` / `stopped`) the master appends one JSON line to `{TASKQUEUE_HOME}/notify.jsonl` and best-effort `POST`s the same payload to this URL (4s timeout, never blocks or fails the run). **Loopback hosts only** by default (`127.0.0.1`, `localhost`, `::1`); remote hosts require `tq config set notify_allow_remote=1`. Omit the field for no notify. A global fallback URL can be set via `tq config set notify_url=http://…` — any task without its own field uses it. |
| `tags` | `[]` | Free-form string labels for filtering: `tags: ["gpu", "overnight"]`. Filter with `tq list --tag gpu --tag overnight` (AND logic). |
| `suite` | `""` | Logical group name. Enables `tq suite enable <suite>` / `tq suite disable <suite>` / `tq suite retry <suite>` to act on all member tasks at once. **Suite retry re-queues completed tasks too** (same statuses as `tq retry`). |
| `env` | `{}` | Extra environment variables merged into the subprocess environment: `env: {MY_VAR: "value"}`. Merged on top of the inherited shell env; existing vars are not removed. |
| `idempotent` | `false` | When `true`, the task is safe to re-run from scratch. Shorthand for `execution_semantics: idempotent`. Enables automatic re-queue when the daemon detects a hung remote run, and when a remote lease expires. |
| `execution_semantics` | from `idempotent` | One of `idempotent`, `checkpointable`, `non_idempotent`, `at_most_once`. Controls what happens when an attempt is lost (lease expiry, hung remote). `idempotent` / `checkpointable` re-queue; `non_idempotent` / `at_most_once` set status **`unknown`** (operator must `tq retry` — never auto-restart). Default is `non_idempotent` unless `idempotent: true`. If both fields are set, `execution_semantics` wins. |
| `auto_retry_on_cap` | `false` | When `true` and `execution_semantics` is `checkpointable` or `idempotent`, a wall-clock cap hit (`interrupted` / `tq.run.cap`) automatically re-queues the task (preferring `resume_args` on the next start). Requires a cooperative/checkpointing script. |
| `max_auto_retries` | `20` | Cap on automatic cap-hit re-queues when `auto_retry_on_cap` is set. `0` = unlimited. When exhausted the task stays `interrupted` with `block_reason=cap_auto_retries exhausted`. Manual `tq retry` resets the counter. Surfaced in `tq list` as `cap×N/M`. |
| `hung_task_k` | unset | Per-task override for the global `hung_task_k` config. Set to a larger value (e.g. `60`) for tasks with long silent phases (LLM inference, single-key translation). `0` disables hung detection for this task. When unset, the global config value (default 5) is used. |
| `group` | `""` | Task group name. Multiple tasks can share a group for bulk operations: `tq group list`, `tq group stop <group>`, `tq group retry <group>`. Different from `suite` (which controls enable/disable); `group` is for operational grouping of related experiments. |

### `suite` vs `group`

Both field group tasks for bulk operations but with different semantics:

| | `suite` | `group` |
|-|---------|---------|
| **Purpose** | Lifecycle management (enable/disable/retry cohorts) | Operational grouping (stop/retry running experiments) |
| **Bulk ops** | `tq suite enable`, `tq suite disable`, `tq suite retry` | `tq group stop`, `tq group retry`, `tq group list` |
| **`retry` scope** | Re-queues all members including `completed` | Re-queues terminal members (same as `tq retry`) |
| **Typical use** | Overnight experiment sets where you want to pause or restart the whole set | Related experiments you might need to stop together mid-run |

A task can have both fields set. Use `suite` when you need to pause or re-arm an entire set; use `group` when you need to stop or retry a running experiment cluster.
| `dry_run` | `false` | When `true`, sync/validate still accept the YAML and the scheduler **will dispatch** it; the subprocess gets `TQ_DRY_RUN=1` so scripts can no-op or skip side effects. Prefer `enabled: false` (or omit from overnight suites) if you need a rehearsal YAML that must never run. |
| `metadata` | `{}` | Arbitrary key-value map stored with the task. Not interpreted by the scheduler; useful for experiment tracking metadata (e.g. `metadata: {config: "v3", seed: 42}`). |
| `artifacts` | `[]` | Paths (relative to project root) that must exist after the run completes. Finalize checks presence; missing paths mark the run as `failed` with `tq.run.artifacts_missing`. |
| `artifacts_required` | `false` | Set `true` to fail (not just warn) when declared artifact paths are missing after the run. |
| `depends_on` (cross-project) | — | Use `project/task_id` syntax to depend on a task in another project: `depends_on: ["other_project/upstream_task"]`. Both same-project (`task_id`) and cross-project (`project/task_id`) refs can be mixed in the same list. |
| `transfer` | `[]` | File-transfer specs for remote dispatch. Each entry: `{src: "relative/path", dst: "relative/path", direction: "push\|pull"}`. `push` copies a file from the master to the worker **before** the run starts (into the attempt `work_root`); failure aborts the claim. `pull` copies from the worker back to the master after the run — prefers files under `work_root` (git_patch/bundle), then `project.root`. Paths are relative to those roots. Ignored for local dispatch. **F-065:** master transfer HTTP requires the active `run_id` + `finalize_token` and only serves/accepts paths declared here. |

| `requires_services` | `[]` | List of service health checks that must pass before the task is dispatched. Each entry may be a **shorthand string** (`"ollama"`, `"docker"`, `"comfyui"`, `"stable-diffusion-webui"`) or a full dict. Shorthand expands to `{name, health_url, timeout_sec}` only. Full dict fields: `health_url`, `timeout_sec`, `min_vram_free_gib` (block when measured free VRAM is below this — uses Ollama `/api/ps` `size_vram` sum vs device total), `resident_ok_models` (list of Ollama model name prefixes; when **every** loaded model matches a prefix, the VRAM gate passes even if free &lt; `min_vram_free_gib` — model already warm), `vram_probe_url`, `vram_total_gib`. If a probe fails the task stays **queued** with `last_error_code: tq.block.service_down` or `tq.block.service_vram` and the reason in `block_reason`. The probe runs every scheduler tick until the service is ready. |
| `overridable_args` | `[]` | List of flag names (e.g. `[--locales, --dry-run]`) that callers may override for a single run via `tq retry project/task --set --locales=pl`. Overrides apply to that run only and never mutate the YAML. Any flag not on this list is refused with a clear error. |
| `matrix` | unset | Expand one YAML into N tasks at `tq sync` time. Each axis is a key with a list of values: `matrix: {seed: [0, 1, 2, 3, 4]}`. The scheduler creates tasks with IDs `{base_id}_seed0` … `{base_id}_seed4`. Each value is appended to `args` as `--seed=N` and merged into `metadata`. Multiple axes are supported (task IDs become `{base_id}_{index}`). |

#### Multi-document YAML

A single `.yaml` file may contain **multiple independent tasks** separated by `---`:

```yaml
task_id: preprocess
command: python
args: [scripts/preprocess.py]
---
task_id: train
command: python
args: [scripts/train.py]
```

`tq sync` and `tq add` call `parse_task_yaml_many`, which returns one `QueueTask` per document. Use this when several closely-related tasks share a file but do not form a `matrix`. Each document must be a complete, valid task spec. The legacy key fixer (`tq validate --fix`) does **not** support multi-document files — split them first.

> **Custom keys:** Any unrecognised top-level key raises a validation error. To attach project-specific data use the `x-` prefix (e.g. `x-hypothesis: "..."`) or store it under `metadata:`. Both survive `tq validate`. The `x-` convention follows YAML extension practice and is ignored by the scheduler.

### Common mistakes (legacy keys)

These top-level names are **not** valid and fail `tq validate` / `tq sync`. Rewrite them (or run `tq validate --fix`, which may drop YAML comments):

| Wrong (top-level) | Correct |
|---|---|
| `ram_gib` | `resources.ram_gib` |
| `timeout_s` | `wall_clock_cap_sec` |
| `hypothesis` | `x-hypothesis` (or under `metadata:`) |
| `project` | omit — project comes from registration / queue dir |
| `name` | `description` (or `x-name` if `description` is already set) |

Minimal example:

```yaml
task_id: example_pilot
description: Short pilot — verify args before overnight
command: python
args: ["scripts/run.py", "--config", "experiments/configs/pilot.yaml"]
resume_args: ["scripts/run.py", "--config", "experiments/configs/pilot.yaml", "--resume"]
wall_clock_cap_sec: 3600
priority: 60
enabled: true
resources:
  cpu_cores: 4
  ram_gib: 8.0
dispatch: local
```

More annotated examples, runnable as-is against `fixtures/tqdemo` (no GPU/CUDA needed — see [`fixtures/tqdemo/README.md`](../fixtures/tqdemo/README.md)):

```yaml
# depends_on: blocked until dummy-deps-a completes successfully.
task_id: dummy-deps-b
command: python
args: ["scripts/dummy_deps.py"]
depends_on: [dummy-deps-a]
```

```yaml
# depends_on_any: run when any dataset producer finishes (OR gate).
task_id: train_first_ready
command: python
args: ["scripts/train.py"]
depends_on_any: [dataset_v1, dataset_v2, dataset_v3]
```

```yaml
# dispatch: remote — prefer online workers; falls back to local if none
# are online. GPU/exclusive — first-fit against the worker's per-device VRAM
# (see §3), and no other exclusive task may run concurrently anywhere.
task_id: gpu_pilot
command: python
args: ["scripts/run.py"]
dispatch: remote
requires:
  os: linux
  min_ram_gib: 16
resources:
  cpu_cores: 4
  ram_gib: 16.0
  gpu_vram_gib: 10.0
exclusive: true
```

Also see: `fixtures/tqdemo/experiments/operator_queue/dummy-exclusive.yaml`
(global exclusive gating), `dummy-exclusive-project.yaml` (per-project exclusive),
`examples/minimal/` (smallest possible working task),
and run `tq validate` before `tq add`/`tq sync` to lint a YAML for common
mistakes (unknown fields, missing command, bad resource types).

### Operator controls (not YAML fields)

| Control | Effect |
|---------|--------|
| `tq freeze` / UI **Block project** | Per-project: no **new starts** (`tq.block.frozen`). Running continue. |
| `tq pause` | Softer skip of new starts (`tq.block.paused`). |
| `group:` + Queue group chips / `tq group …` | Operational cluster + UI filter; **not** an admission weight. `tq group retry` re-queues completed members. |
| `suite:` + `tq suite …` | Enable/disable/retry a labeled batch (retry includes completed). |
| `tq retry … --downstream` | Re-queue this task and all same-project transitive `depends_on` dependents. |
| `tq force` | One-shot soft/anti-flap bypass; UI toast confirms the re-queue. |

Do not confuse project freeze with the idle **FROZEN** badge on a running card (no recent log output for `frozen_threshold_sec`).

---

## 1. Progress Reporting Protocol

To allow developers and the web UI to monitor running tasks, all task scripts **must** report their progress.

### The Progress Format
The `taskqueue` daemon monitors the standard output (stdout) of running tasks. It looks for progress patterns formatted as `(XX.X%)` or `(XX%)` (using the regular expression `\((\d+(?:\.\d+)?)%\)`).

* **Requirement**: Print your current progress percentage enclosed in parentheses, followed by a percent sign, e.g., `(45.2%)` or `(70%)`.
* **Example Output**:
  ```
  [epoch 4/10] Loss: 0.124 (40.0%)
  ```

### Structured Metrics (TQ_METRIC)

For richer monitoring, tasks can emit structured key-value metrics alongside regular progress lines. The daemon parses any stdout line that starts with `TQ_METRIC ` followed by a JSON object:

```
TQ_METRIC {"epoch": 3, "loss": 0.124, "etl_at_3": 0.14}
```

* Each emission **merges** its keys into the run's metric store (last value per key wins).
* The accumulated metrics are stored in `runs.metrics_json` and exposed by the `progress_snapshot` MCP tool as `metrics: {...}`.
* Keys are arbitrary; use whatever names make sense for your experiment (e.g. `loss`, `val_acc`, `etl_at_epoch`).
* Numeric values are also kept as a short time series (last 60 samples per key, first 8 keys) under the reserved key `_series`; the task drawer in the web UI draws them as sparklines. `_series` and `log` cannot be set by the task, and run diffs ignore `_series`. Remote-worker runs record them too (the master parses each log chunk).

Both clients have a helper (tests check their output against the runner's parser):
```python
import tq_client as tq
tq.metric(epoch=3, loss=0.124)          # TQ_METRIC {"epoch": 3, "loss": 0.124}
```
```cpp
tq::metric({{"epoch", 3}, {"loss", 0.124}});   // numeric values; keys are JSON-escaped
```

### stdout Buffering
By default, Python buffers stdout when redirected to a pipe or file, which delays progress updates.
* **Requirement**: Ensure stdout is flushed immediately.
* **How-to**:
  * In Python scripts, use `print("...", flush=True)` or run python with `PYTHONUNBUFFERED=1` (the task runner automatically sets `PYTHONUNBUFFERED=1` in the environment, but flushing explicitly is still a best practice).
  * In other languages or shell scripts, use equivalent unbuffered printing utilities.
* **Child and grandchild processes.** The task's stdout/stderr (the log file on Windows, a pipe elsewhere) is inherited by processes the task starts, so their output reaches the run log as long as they do not redirect it. Two things break that:
  * **Capturing it.** `subprocess.run(..., capture_output=True)` or `stdout=PIPE` keeps the child's output in your process; nothing reaches the log until you print it. Either let the child inherit stdout (`subprocess.run(cmd)` with no stdout argument), or relay it line by line:
    ```python
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        print(line, end="", flush=True)   # progress lines like "(42.0%)" now reach tq
    p.wait()
    ```
  * **Native buffering.** A C/C++/Go/Rust program writing to a file or pipe (not a console) usually block-buffers stdout, so `(XX.X%)` lines appear in large batches or only at exit. `PYTHONUNBUFFERED=1`, which tq sets, affects Python processes only. Flush after each progress line in the native program (`std::cout << std::endl` / `fflush(stdout)` / `setvbuf(stdout, NULL, _IOLBF, 0)` at start; the C++ client's `tq::progress` already flushes), or relay as above.

### Estimated / Non-Deterministic Progress
If a task's exact iteration count is unknown, the script must calculate a best-effort estimate:
* **Time-based**: Calculate progress based on elapsed time vs. expected timeout.
* **Stage-based**: If there are 5 sequential stages, assign each stage a fixed percentage (e.g., Stage 1: 0% to 20%, Stage 2: 20% to 50%, etc.) and interpolate within each stage.
* **Decaying Estimate**: Start at 0% and approach 99% asymptotically (e.g., $P = 100 \times (1 - e^{-kt})$) to indicate the task is alive and working, though this should be a last resort.

---

## 2. Checkpointing and Resume Protocol

Long-running tasks (>10 minutes) are susceptible to interrupts from machine reboots, daemon restarts, power loss, or wall-clock caps. Therefore, **they must be restartable from the last saved state**.

### Checkpoint Frequency
* **Rule**: A checkpoint is **due** when **either** condition holds since the last successful write: (1) accumulated work units reach a caller-chosen grain (`every_n_units` — epoch, 1 000 steps, batch, …), **or** (2) wall clock reaches ~10 minutes (`every_sec`, default **600**). Write once, then **reset both** counters so work and time cannot fire a second write back-to-back. Prefer `CheckpointCadence` in [`clients/python/tq_client.py`](../clients/python/tq_client.py) / [`clients/cpp/tq_client.hpp`](../clients/cpp/tq_client.hpp). Force-saves on cooperative suspend / SIGTERM always write, then `reset()` — they do not go through `due()`.
* **Location**: Store checkpoints in a task-specific subdirectory, e.g., `<project_root>/checkpoints/<task_id>/`.

### The `resume_args` Mechanism
Task YAML files support a `resume_args` field. When a task is interrupted, its last run status is `interrupted`. On the next claim/start, the master (local) and workers use `resume_args` **only** when that last run was interrupted — not merely because `resume_args` is present in the YAML.

#### Task YAML Setup Example
```yaml
task_id: train_cfr_model_001
command: python
args: ["scripts/train.py", "--epochs", "100", "--out-dir", "runs/cfr_001"]
resume_args: ["scripts/train.py", "--epochs", "100", "--out-dir", "runs/cfr_001", "--resume"]
resources:
  cpu_cores: 4
  gpu_vram_gib: 4.0
```

#### Task Script Implementation
Your script must handle the `--resume` flag:
1. **No `--resume` flag**: Initialize the model/state from scratch.
2. **`--resume` flag present**: Check if a checkpoint exists in the output directory.
   * If yes: Load the checkpoint, restore the iteration counter (e.g., start training at epoch 42 instead of 0), and continue.
   * If no: Log a warning and fall back to starting from scratch (safe fallback).

### Graceful Shutdown (SIGTERM Handling)
When a task is stopped via the CLI/UI, hits `wall_clock_cap_sec`, or is evicted for host **RAM/VRAM hard** pressure, the runner terminates the **entire process tree** (wrapper + descendants). This matters for Windows YAMLs that use `cmd /c scripts\py …`: killing only `cmd.exe` used to leave the Python child holding RSS after `tq.stop.ram`.

The tree gets `SIGTERM`/`terminate`, then after `graceful_stop_sec` (default **30** for cooperative stop; **2s** for RAM/VRAM hard eviction) any survivors are `SIGKILL`/`kill`.

* **Requirement**: Catch `SIGTERM` (and `SIGBREAK` on Windows) to save state immediately and exit cleanly with code `0` or a non-failure code.
* **Example Python Implementation**:
  ```python
  import signal
  import sys

  class TrainingLoop:
      def __init__(self):
          self.should_stop = False
          signal.signal(signal.SIGTERM, self.handle_shutdown)
          if hasattr(signal, "SIGBREAK"):
              signal.signal(signal.SIGBREAK, self.handle_shutdown)

      def handle_shutdown(self, signum, frame):
          print("Received termination signal! Requesting graceful checkpoint...", flush=True)
          self.should_stop = True

      def run(self):
          for epoch in range(total_epochs):
              if self.should_stop:
                  self.save_checkpoint(epoch, final=True)
                  sys.exit(0)
              
              # ... perform work ...
              
              if epoch % 5 == 0:
                  self.save_checkpoint(epoch)
  ```

---

## 3. Resource Allocation Constraints

Because multiple projects share the same hardware instance, task specifications should declare realistic resource **hints**.

| Field | Meaning | Standard |
| :--- | :--- | :--- |
| `cpu_cores` | Number of CPU logical cores required. | Defaults to `1`. Restrict multi-threaded/parallel code to this limit. |
| `ram_gib` | Expected RAM GiB. | A **floor**, not a cap: the scheduler admits against `max(declared, predicted-from-history, default_task_ram_gib)`, so under-declaring does not buy you a free pass. Prefer observed peak RSS, not process `--ram-cap` values. See [`scheduler.md`](scheduler.md). |
| `gpu_vram_gib` | VRAM required in Gigabytes. | Critical for sharing our 6 GB GPU. If a task needs the entire GPU, specify `gpu_vram_gib: 6.0` to make it exclusive. |

**GPU VRAM — admission guard vs. device state (important for Ollama / vLLM):**

`gpu_vram_gib` is an *admission guard*: the scheduler will not start a task unless a GPU device has at least that many GiB free *according to the current reservation accounting*. It does **not** guarantee the device is physically empty when the task starts. External ML runtimes such as Ollama and vLLM retain model weights in VRAM beyond the task's lifetime via `keep_alive` — the previous task's reservation was released but the weights may still occupy the card.

**Rule for tasks backed by Ollama or vLLM:** set `exclusive: true` (or `gpu_vram_gib: <full card size>`) to prevent any other GPU task from being admitted concurrently. This ensures your task is the only one that needs to load weights, and the runtime's `keep_alive` window does not cause a collision:

```yaml
resources:
  gpu_vram_gib: 5.4   # model footprint
exclusive: true        # prevents overlap while Ollama keeps weights loaded
```

If you need CPU-only tasks to run concurrently while the GPU job runs, `exclusive: true` is too broad — it blocks all tasks. Prefer either:

- maximize `gpu_vram_gib` to the full card size so no second GPU task is admitted while CPU-only tasks (0 VRAM) remain schedulable, or
- use `exclusive_project: true` when you only need solo execution within that project (other projects may still run).

**Remote P6 hold (automatic):** when a large top-of-queue task cannot start yet but will fit a worker’s total capacity, the scheduler temporarily holds that estimate on the roomiest worker so small tasks cannot fill the gap. This is **not** a substitute for `exclusive` / `exclusive_project` — it does not flip YAML and expires with `reservation_max_ticks`. See [scheduler.md](scheduler.md) §3 and [master_worker.md](master_worker.md).

**GPU task runtime conventions:**

- **`--require-gpu` flag**: Any YAML with `gpu_vram_gib > 0` should pass `--require-gpu` (or the project's equivalent) in both `args` and `resume_args`. This causes the script to check `torch.cuda.is_available()` at startup and exit with a clear error if CUDA is unavailable, rather than silently falling back to CPU. Silent CPU fallback produces valid-looking output at ~10× longer training time — without this guard the first sign of a problem may be the daemon's VRAM-idle warning many minutes later.

- **VRAM-idle warning**: If a task declares `gpu_vram_gib > 0` but its peak VRAM is below 0.1 GiB after the settle window (default 180s), the daemon emits a `WARN` in `daemon.log`: the model is likely running on CPU. Peak VRAM includes the task process tree **and** sidecar processes named in `resources.locks` / `requires_services` (so an Ollama job is no longer reported as 0.0 GiB). Check with `tq log --grep WARN`. Local runs sample live VRAM/GPU %; remote workers report sidecar peak VRAM at finalize (live remote GPU % is not in the heartbeat yet).

- **GPU utilization EMA monitoring**: The daemon tracks a per-task GPU utilization exponential moving average (alpha=0.3). After ≥3 samples, if the EMA is below `gpu_util_warn_pct` (default 15%), a pipeline CPU-bottleneck warning is emitted. Instantaneous util is also stored in `resource_history.gpu_util_pct` and shown in the UI / `tq metrics` / MCP `progress_snapshot`. Common causes: data not in mmap/pinned format, `num_workers=0`, small batch size, non-contiguous tensors. Full cause/fix table: [`scheduler.md §6`](scheduler.md). Tune the threshold: `tq config set gpu_util_warn_pct=20`.

- **`tq doctor` GPU diagnostics**: reports per-device VRAM (used/free), utilization, driver version, temperature, and `torch_cuda` availability. Detects driver–toolkit mismatches. Use before overnight GPU runs to confirm the stack is healthy.

**Post-exit shell (`teardown`) — flush OS page cache after mmap-heavy training:**

Tasks that read large `np.memmap` caches (e.g. a 22 GiB training cache) fill the Windows standby list. After the process exits, standby pages remain counted in `psutil.virtual_memory().used`, which blocks tq admission for subsequent tasks. Use a `teardown` hook to flush:

```yaml
teardown:
  command: cmd
  args: ["/c", "scripts\\py", "python", "scripts/flush_page_cache.py"]
  best_effort: true
  timeout_sec: 120
```

The script exits 0 even when not elevated (advisory only). On Linux it writes `3` to `/proc/sys/vm/drop_caches` (requires root). The master runs teardown **asynchronously** after finalize so queue handoff is not blocked; RAM may still look high until flush completes. Use this pattern for mmap-heavy training YAMLs. See [`scheduler.md §6`](scheduler.md) for the full memory management operator guide including stray-process diagnosis.

**Host RAM gates (config):** full detail in [`scheduler.md`](scheduler.md).
* `ram_limit_pct` (**soft**): the master does not start a task whose estimate would push host used RAM above this fraction of total. The gate is **additive** — it asks whether admitting *this* task would exceed the limit, not whether the host is already over.
* `ram_hard_limit_pct` (**hard**, default 95, must be ≥ soft): while above hard, the master gracefully stops tasks → status `stopped` / `tq.stop.ram`, until the projection falls under soft (at most `stop_max_per_tick` per tick). The victim is chosen by cost-of-killing — progress, priority, elapsed time and **restartability** (`idempotent`, `resume_args`) all count, not just RSS. Admission then freezes for `recovery_cooldown_sec`. When soft clears, the scheduler may restart it unless anti-flap predicts another hard hit (`live_used + max(hint, last_peak) > hard`). Use `tq force` to bypass.

* **Avoid Over-specifying CPU/GPU**: still blocks packing.
* **RAM hints**: keep them near real peaks. Under-declaring is not rewarded (history and the `default_task_ram_gib` floor override it) but over-declaring does hold back admission.
* **`max_concurrent_tasks`** (default 0 = unlimited): hard cap on how many tasks may run locally at once. When reached, no new local tasks are started that tick regardless of available CPU/RAM. Set with `tq config set max_concurrent_tasks=N`; `0` removes the cap.

### Real/Peak Memory Tracking and Profiling

The `taskqueue` daemon dynamically monitors and records the actual resource usage of tasks:
* **RAM (CPU)**: Tracks the sum of Resident Set Size (RSS) for the main task process and all of its child processes recursively using `psutil`. Live RSS also picks the hard-limit eviction victim.
* **VRAM (GPU)**: Queries `nvidia-smi` compute-apps. Counts CUDA memory for PIDs in the task process tree **or** whose name/cmdline matches `resources.locks` / `requires_services[].name` (sidecar runtimes such as Ollama; `ollama` also matches `llama-server`). Windows WDDM often reports per-process VRAM as `[N/A]` — then the sample falls back to device `memory.used` (same shared-device caveat as GPU %). A failed `nvidia-smi` keeps the previous reading instead of storing 0 GiB. CPU-only tasks skip this probe. Device GPU % is sampled while attributed VRAM is > 0 (two concurrent GPU tasks share the same device %).
* **Log issue counts**: each stdout line is classified (`ERROR`/`FATAL`/`CRITICAL`/`REJECT` → errors, `WARN`/`WARNING` → warnings, `LOW SCORE` → other). Counts live in `runs.metrics_json.log` and show as chips in the UI / `tq metrics` / MCP `progress_snapshot` when non-zero. English “error” / `0 errors` is ignored. Tasks cannot overwrite the reserved `log` key via `TQ_METRIC`.
* **Peak Metrics**: The maximum values observed during execution are recorded in the SQLite database and rendered in the Web UI:
  - **Dashboard (Running)**: Displays the current running peak RAM/VRAM.
  - **Queue Tab**: Displays the peak RAM/VRAM from the last completed run next to the requested resource tags, allowing you to easily adjust your YAML declarations.
  - **History Tab**: Records the peak RAM/VRAM reached by prior runs.

**Resource history sampling (daemon config):** the daemon records a `(cpu_pct, ram_gib, vram_gib)` sample to the `resource_history` table every `resource_sample_sec` seconds (default: `10`). Samples older than 7 days are pruned hourly. The Web UI renders these as a sparkline chart when you expand a task's log.

**RSS trend (memory-growth) detection:** after `ram_settle_sec` (default 1800s = 30 min), the daemon fits an OLS line over a rolling 10-minute window of RSS samples and warns if the slope exceeds `ram_leak_warn_gib_per_hour` (default 1.0 GiB/h). The 30-minute settle is intentionally longer than the GPU settle window (3 min) — initial model/mmap load and multi-stage shard rotations (load shard → train → free → next shard) produce large legitimate RSS swings that average out over 30 min:

```
[queue] WARN <task> RSS growing at 2.34 GiB/h (threshold 1.0 GiB/h, current_rss=14.2 GiB, elapsed=22m03s).
Possible memory leak or unbounded cache growth. ...
```

Common causes in ML training: accumulated Python batch lists, `np.memmap` standby not released between epochs, PyTorch gradient graph not freed (`loss.backward()` without `zero_grad`). If the growth is benign (e.g. a one-time pre-load that plateaus), bump the threshold: `tq config set ram_leak_warn_gib_per_hour=5`. The warning fires at most once per run (no log spam).

Progress / suspend helpers for task scripts: [`clients/`](../clients/README.md) (Python + C++).

### Typical Task Footprints (Reference Profiles)

Refer to these estimated footprints for standard antigravity tasks to align your YAML resource parameters:
* **Large in-memory grids / blueprints**: often **~8.0 GiB RAM**.
* **Batch equity / Monte Carlo style jobs**: often **~2.0–4.0 GiB RAM**.
* **CPU-heavy solver sweeps**: often **~1.0–2.0 GiB RAM** (light RAM, heavy CPU).
* **Search engines with model weights**: often **~2.0–6.0 GiB RAM**.

---

## 4. Portability and Distributed Execution (Wave 1+)

With Wave 1, tasks may be executed on remote worker machines. To ensure a task runs successfully on a worker node:

### 4.1. Use Portable Commands
* **Python Scripts**: Always use `command: python`. Never hardcode absolute virtualenv paths (e.g. `d:\antigravity\taskqueue\.venv\Scripts\python.exe`). The task runner automatically resolves `python` to the project's own `.venv` Python executable if it exists.
* **Compiled Binaries**: Use project-relative paths and omit the `.exe` extension (e.g. `command: bin/agents/my_tool`). The runner automatically appends the correct extension depending on the worker's operating system.
* **Directory Separators**: Use forward slashes `/` for paths in arguments and commands. The Python path libraries handle conversions.

### 4.2. Declare Dispatch Eligibility
Specify where the task is allowed to run:
* `dispatch: local` (Default): Task must run on the development/master machine only. Use for build pipelines, WSL tasks, or fast debug runs.
* `dispatch: remote`: **Hint** — prefer an online remote worker. If **no** workers are online, the master schedules locally (still subject to `requires` / resources / deps). Do not use `remote` to “park” a task when you have no remote fleet.
* `dispatch: any`: Task runs on whichever machine becomes free first (master or worker).

### 4.3. Declare Hardware Requirements
Use the `requires:` block to enforce constraints on the worker node:
```yaml
requires:
  os: windows          # windows | linux | macos | any
  arch: x86_64         # x86_64 | arm64 | any
  min_ram_gib: 8.0     # worker must have at least this much total RAM
```

---

## 5. Other Requirements & Best Practices

### Concurrent build races — do not inline project build scripts

When multiple queue tasks each call a shared build script (e.g. `wsl_build.sh`) as part
of their `args`/`resume_args`, they may overlap: task A completes its build, releases
the build lock, and starts executing the binary — at which point task B starts its own
build and relinks the same binary while task A has it open for exec. Linux/WSL reports
`OSError: [Errno 26] Text file busy`; Windows would see a similar sharing-violation.

**Rule:** never inline a project build script directly in task `args` when the same
binary could be running in another concurrent task. Instead provide a project-level
wrapper script that holds a `flock` (or equivalent) across both the build *and* the
subsequent exec, and call that wrapper from the YAML. The lock ensures: (a) only one
build runs at a time, and (b) no task can start rebuilding while another task has the
resulting binary open. Provide a project-level wrapper (flock around build+exec) and call
it from the YAML. Resume tasks that skip the build (`--no-build`) should NOT acquire the
exec lock since they're fine running the already-built binary.

### Headless Execution
* **Rule**: Tasks must run completely non-interactively.
* Do not call `input()`, prompt for confirmation, or wait for user keypresses. Any task blocking on stdin will eventually hit `wall_clock_cap_sec` or be terminated due to idleness.

### Path Resolution
* **Rule**: All commands, scripts, and inputs/outputs must use paths relative to the project root directory.
* The task runner sets the current working directory (`cwd`) to the project's root folder before launching. Avoid using absolute paths or paths relative to home directories unless configured dynamically.

### Wall-Clock Timeouts
* **Rule**: Set `wall_clock_cap_sec` in the YAML for any task that could potentially hang or run infinitely (e.g., network fetches, experimental CFR runs with unknown convergence).
* Setting a cap prevents a locked process from permanently consuming a queue execution slot.
* **Outcome:** when the daemon/worker kills a run for the cap, the task is recorded as **`interrupted`** with **`tq.run.cap`** (and `stop_reason=wall_clock_cap`). A real non-zero process exit without a cap kill is **`failed`** / **`tq.run.nonzero`**. Use `tq list` — the bracketed code distinguishes them; do not assume every non-zero exit needs investigation.
* **Opt-in auto-retry:** set `auto_retry_on_cap: true` with `execution_semantics: checkpointable` (or `idempotent`) so cap hits re-queue automatically (preferring `resume_args`). Bound grinding with `max_auto_retries` (default 20; `0` = unlimited). `tq list` shows `cap×N/M` while grinding. Exhaustion leaves the task `interrupted` with `block_reason=cap_auto_retries exhausted`; `tq retry` resets the counter.
* Without `auto_retry_on_cap`, use `tq retry` (or `tq retry --project P --status interrupted`) after a cap hit.

### Dependency Management (`depends_on` / `depends_on_any`)

**One pipeline model.** Use these cases:

| Need | What to do |
|------|------------|
| **Sequence** | Consumer `depends_on: [producer]`. |
| **Verify then continue** | Insert a **check** task between them; consumer `depends_on: [check]`. The check script reads artifacts/metrics and exits 0/nonzero. If the check fails, downstream stays blocked (`dead dependency`) until you fix and retry the **check**. |
| **Fan-in (any upstream)** | Consumer `depends_on_any: [dataset_v1, dataset_v2, dataset_v3]` — admits when **any** peer completes. Use when one of several producers is enough. |
| **Choose B or C** | Pre-author both with `enabled: false` (both may `depends_on` the same upstream or check). After notify, enable **one** via `tq patch … enabled=true` / MCP `patch_task`. |
| **Re-run a whole set** | Put the same `group:` or `suite:` on every member. Batch retry re-queues the set **including completed** (`tq group retry`, `tq suite retry`). For a dependency chain, prefer `tq retry <root> --downstream` so `depends_on` still orders starts. |

**Rules**

- If Task B processes the results of Task A, declare `depends_on: [task_a]`. The scheduler guarantees Task B will not start (or resume from checkpoint) until Task A finishes successfully. If Task A fails, Task B will remain blocked.
- **`depends_on_any`:** OR gate — ready when any listed peer is `completed`. Broken only when every peer is missing or dead (`failed` / `stopped` / `interrupted` / `disabled`). Cross-project refs (`project/task_id`) work the same as AND deps.
- **`on_dependency_failure: block | cancel`** (default `block`): with `cancel`, a *queued* dependent is moved to the terminal status `cancelled` (`tq.dep.cancelled`, reason names the dead peer, notify event emitted) once a `depends_on` peer is `failed`/`cancelled` (for `depends_on_any`: every peer). It cascades one hop per tick through further `cancel` dependents. `stopped`, `interrupted` and `disabled` peers never trigger it (auto-restart / operator action). `tq retry <root> --downstream` revives `cancelled` dependents; `block`-mode dependents of a cancelled task show `dead dependency … [cancelled]`. Use it for gate tasks that exit non-zero by design so kill decisions don't leave zombie `queued` chains.
- **Negative results:** a gate that exits with a code listed in its `negative_exit_codes` ends `completed` with `tq.run.negative` (shown as `completed [tq.run.negative]`, not in the Failed list). Its dependents run as after any completion unless they set **`on_dependency_negative: cancel`**: then a queued dependent becomes `cancelled` (`tq.dep.negative`). Other non-zero exits still fail.
- **Both fields:** a task may set both; admission requires all AND peers **and** at least one OR peer.
- **Running + upstream retry:** if a dependent is already `running` and an upstream is re-queued (`tq retry`), the daemon stops the dependent with `tq.stop.deps` (auto-restartable `stopped`) when deps are no longer satisfied. For OR deps, the stop happens only if no other OR peer remains completed. Admission then waits on `tq.block.deps` until peers complete again; the next start may use `resume_args` when the last run was stopped/interrupted.
- **Missing dependency:** a typo or deleted peer leaves the dependent `queued` with `tq.block.deps` / `waiting on: X (not found)`. Sync does not hard-fail. Use `tq health` (or the Queue banner / red dep chips in the web UI) to find broken chains.
- **Disabled dependency:** if a dependency task is `disabled` (`enabled: false`), the dependent blocks indefinitely — disabled tasks never complete. To unblock: re-enable and run the dependency (`tq patch project/dep_id enabled=true && tq sync`), or remove the dependency from the YAML. Use `tq deps project/task_id` to inspect the full dependency graph and block reason.
- **Re-runnable sets** (tests, localization sweeps): always set `group:` or `suite:` at authoring time so operators can restart the whole batch in one action.

**Advanced (not the default pipeline story):** `on_complete` / `on_fail` re-queue listed task IDs when this task finalizes (local runner). Those re-queues stamp `spawned_by: project/parent_task_id` (runtime lineage; not a YAML field — shown in `tq list --json` / Deps API). Prefer `depends_on` for sequencing. `rearm_on: upstream_success` re-queues dependents when an upstream completes again — niche; also sets `spawned_by`.

### Post-exit shell (`teardown`)

For OS-level cleanup after the process exits (e.g. flush page cache after mmap-heavy training), use `teardown`, not `on_complete`:

```yaml
teardown:
  command: cmd
  args: ["/c", "scripts\\py", "python", "scripts/flush_page_cache.py"]
  best_effort: true
  timeout_sec: 120
```

The script exits 0 even when not elevated (advisory only). On Linux it writes `3` to `/proc/sys/vm/drop_caches` (requires root). Teardown is async — it does not delay starting the next queued task. See [`scheduler.md §6`](scheduler.md) for the full memory management operator guide.

### Idleness / Log Heartbeats
* **Rule**: Print *something* to stdout at least once every 5 minutes.
* The daemon monitors stdout activity. If a task goes silent for too long, it may trigger system warnings or be suspected of deadlocking. Print a basic heartbeat or current progress percentage even during long operations.

### Multi-GPU scheduling

* `detect_system()` enumerates all NVIDIA devices; `/api/capacity` and `tq capacity` expose `gpus[]` with per-device free VRAM.
* Tasks request aggregate `resources.gpu_vram_gib`; the local scheduler first-fit packs onto a device that has enough free VRAM.
* Fair-share v1: at equal priority, projects with fewer currently-running GPU tasks are preferred.

### Progress sanity (active time)

* Freeze is not “no stdout” alone: the daemon also tracks process **CPU active time**.
* A run is frozen when both stdout idle and CPU idle exceed `frozen_threshold_sec`.
* At **2×** that threshold the task gets attention code `tq.run.frozen` (not auto-killed).
* If `(N%)` jumps without matching CPU burn → `tq.run.progress_suspect` (see fixture `dummy-lie-progress`).
* This CPU-burn check is **skipped entirely for tasks that request a GPU** (`resources.gpu_vram_gib > 0`): host CPU time isn't a valid work signal for a task blocked on GPU sync, so it would false-positive on legitimate GPU jobs. Genuine hangs on GPU tasks are still caught by `tq.run.frozen` (no stdout **and** no CPU idle), which doesn't share this failure mode.
* It is also **skipped for WSL-dispatched tasks** (`command: wsl`): the Windows host cannot attribute CPU time to processes inside WSL via `psutil`, so arena/build progress would false-positive as `tq.run.progress_suspect` (F-244). Non-WSL tasks using `command: python` are still checked; see fixture `dummy-lie-progress`.

### Cooperative suspend (control file)

Long GPU/checkpoint jobs should honor a **cooperative suspend** request instead of relying on SIGSTOP.

* The runner sets `TQ_CONTROL_FILE`, `TQ_HOME`, `TQ_PROJECT`, `TQ_TASK_ID` in the task environment.
* `tq suspend project/task_id` writes `{"action": "suspend"}` to that file (no SIGTERM).
* The task should checkpoint, then exit **75**. The daemon records status `suspended`.
* `tq retry` re-queues suspended tasks and uses `resume_args` when the last run was `suspended` or `interrupted`.
* Cap hits (`tq.run.cap`) also use `resume_args` on the next start. With `auto_retry_on_cap: true` the daemon re-queues without a manual `tq retry`.
* Helpers: [`clients/python/tq_client.py`](../clients/python/tq_client.py) (`poll_control`, `maybe_suspend`, `progress`).
* `tq stop` / `wall_clock_cap` write the same control action, then wait **`suspend_grace_sec`** (default 10s, `tq config set suspend_grace_sec=N`) before SIGTERM — a real window for the task to checkpoint and exit 75 on its own. If the task exits cooperatively within that window, it's recorded `suspended` and never receives SIGTERM. Set `suspend_grace_sec=0` to restore the old immediate-SIGTERM behavior.
* **Stop reason**: `tq stop project/task_id --reason gate:lagging` stores a human-readable reason string in the run record. The reason is included in `drain_notifications` events as `stop_reason` so automation can route on why a task was stopped.
* **Requester**: a stop request also records who asked (`TQ_OPERATOR` if set, the channel — cli / mcp / web — and the client) as `requested_by` in the control file, and daemon.log names it once: `cooperative suspend requested p/t by ada via mcp (mcp/cursor 1.0)`. Scheduler-initiated stops (cap, RAM/VRAM pressure, frozen) log their own reason.

### No in-task `tq` enqueue

**Rule:** tasks must not invoke `tq retry`, `tq add`, or `tq sync` against the same daemon from within their own process (or any subprocess they own).

Calling `tq` from inside a queued task:
- hides the spawned work from the DAG (`depends_on` / `on_success` can't see it),
- races with manual `tq retry` or daemon re-queues,
- makes the dependency graph in `tq status` / `tq deps` misleading,
- bypasses `overridable_args` guards.

**Correct patterns:**
- Chain via `depends_on` — the dependent task starts when its dependency completes.
- Use `on_complete` / `on_fail` only as **advanced** wake lists to re-queue specific task IDs — not as the default way to “run the next step” (use `depends_on` for that).
- Write artefacts or progress output to disk; let a separate queued task consume them.
- Agents may use MCP `enqueue_task` / `retry_task` from *outside* the running task.

**Exception:** a task that is a *build step* and needs to launch a binary it just compiled may do so as a subprocess in the same run — that is not re-enqueueing work, it is ordinary process forking and does not require queue coordination.

### End-of-task events (`notify.jsonl`) and the opt-in webhook (`notify_url`)

* On every terminal finalize the master appends one JSON object to `{TASKQUEUE_HOME}/notify.jsonl` — **no YAML field needed** (config `notify_default`, default `1`; `tq config set notify_default=0` restores the old opt-in behaviour). Agents read it with MCP `drain_notifications`.
* `notify_url` (YAML or global config) only adds a best-effort `POST` of the same payload (4s timeout; failures are logged, never fail the run).
* Payload fields: `ts`, `project`, `task_id`, `status`, `run_id`, `exit_code`, `last_error_code`, `started_by`, `notify_url`. Optional enriched fields when set: `submitted_by`, `submitted_via` (task provenance), `description`, `suite`, `elapsed_sec`, `peak_ram_gib`, `progress_pct`, `block_reason`, `stop_reason`.
* Hosts must be loopback unless `tq config set notify_allow_remote=1`.
* Agents wanting to know a task finished: MCP `wait_for_task` / `tq wait` (no `notify_url` needed). `drain_notifications` (optional `since` / `operator` / `project` / `wait_sec`) reads the event log; `operator` matches `started_by` **or** `submitted_by`. MCP cannot push-wake a session. See [`docs/mcp.md`](mcp.md).

### Reading task results

The queue has no result store — output is written by the task script to files.

* **Log output:** `tq logs project/task_id` or MCP `log_tail`. Log lines are retained as long as the task row exists in the DB (removed only by `tq remove`). Daemon restarts do not delete logs.
* **Files:** read paths listed in `artifacts` directly from the project root.
* **Experiment metadata:** fields set in `metadata:` are stored with the task row and are included in `tq list --json` output and MCP `my_tasks`.
* **Structured output:** if the script writes JSON lines to stdout, retrieve them via MCP `log_tail` (up to 500 lines) or `tq logs --jsonl project/task_id`.
* **Per-task run history:** `GET /api/tasks/<project>/<task_id>/runs` (web API) returns all runs with start/end times, exit codes, peak RAM/VRAM, worker, and error category.
* **Was it re-run?** Each task carries `run_number` (1-based count of its runs, 0 = never ran), plus `last_run_id`, `last_run_started_at`, `last_run_finished_at` — in `GET /api/tasks/<project>/<task_id>`, `tq describe`, MCP `wait_for_task` / `task_result`. `run_number` is also on every row of `GET /api/queue` and `GET /api/tasks`. `POST …/retry` and MCP `retry_task` return `run_number_before`; the re-run has started once `run_number` exceeds it (while `queued`, the `last_run_*` fields still describe the previous run).

### Extended JSON output (`tq list --json`)

`tq list --json` returns a full object per task including:

| Field | Description |
|-------|-------------|
| `enabled` | DB enabled flag after last sync/patch (`true`/`false`). Independent of `status` — a task can be `enabled` and still `completed` until `tq retry`. |
| `progress_pct` | Latest run progress from `(N%)` heartbeats (`null` if unknown) |
| `requires_gpu` | True when `resources.gpu_vram_gib > 0` |
| `metadata` | The task's `metadata:` dict verbatim |
| `artifacts` | List of `{path, exists, size, mtime}` — paths declared in `artifacts:` with live disk stats |
| `depends_on` | AND peers — all must complete |
| `depends_on_any` | OR peers — any one must complete |
| `spawned_by` | Parent `project/task_id` that last re-queued this task via `on_complete` / `on_fail` / `rearm_on` (`null` if never) |
| `exit_code` | Exit code of the most recent run (`null` if never run) |
| `attempts` | Total number of run records for this task |
| `started_at` | ISO timestamp of the most recent run start |
| `finished_at` | ISO timestamp of the most recent run end |
| `log_path` | Absolute path to the most recent run's log file |
| `last_error_code` | Error code from the task row |
| `block_reason` | Block reason string from the task row |
| `last_log_line` | Last captured stdout line from the latest run |
| `lock_waiting_on` | When blocked on `tq.block.mutex`: list of `{lock, holder, holder_started_at}` |

**Pipeline restart:** `tq reverify <project/root> --up-to <task_id>` enables tasks on the path from root to target, disables other downstream of root, then retries with a skip report. Prefer this over manual patch + `tq retry --downstream`. Inspect contention with `tq locks`.

**Programmatic result:** `tq result <project/task_id>` prints the last non-empty line of the latest log and exits **0** iff `status=completed` (else **1**). Prefer this over `tq tail` in non-interactive sessions. `tq tail … --last N` dumps the last N lines and exits immediately (no follow).

### Validation errors

Task YAML is validated in one place (`validate_task_spec` in `taskqueue/db.py`) before it is queued. An empty file, a non-mapping document, a missing or blank `command`, a `task_id` containing `\`, `..` or control characters, or negative `wall_clock_cap_sec` / `max_attempts` / `resources.*` values are rejected with a message naming the file and field. A missing `task_id` is reported as a missing-field error.

To check YAML without queueing it, `POST /api/validate` with `{"project": ..., "yaml_text": ...}`. It runs the same parser in a scratch directory and returns `{"ok": true, "valid": true, "task_ids": [...]}` or `{"ok": true, "valid": false, "error": "..."}`; nothing is written to the queue.
