# Master–worker configuration

Full guide for configuring a **master** daemon and one or more **worker** agents so remote machines can claim and run queue tasks.

| Doc | Role |
|-----|------|
| **This page** | Configure master + worker (homes, bind, auth, YAML files, CLI) |
| [master_worker.md](master_worker.md) | How pull scheduling, sync, logs, and disconnect recovery work |
| [task_standards.md](task_standards.md) §4 | YAML `dispatch` / `requires` / `transfer` / `code` |
| [errors.md](errors.md) | `tq.infra.*` disconnect codes |

---

## Quick start — step by step

Use this path once for a normal setup (master port **8765**, default home `~/.taskqueue`).

### A. On the master machine

1. **Install and start**
   ```powershell
   cd path\to\taskqueue
   uv sync
   uv run tq start
   uv run tq doctor
   ```
   Check that the daemon is running and the port is `8765`.

2. **Open the API for remote machines**
   ```powershell
   uv run tq config set bind_host=0.0.0.0
   uv run tq restart
   ```
   Prefer [Tailscale](https://tailscale.com/). Note the master Tailscale IP (e.g. `100.64.0.1`).

3. **Register projects** (edit `%USERPROFILE%\.taskqueue\projects.yaml`):
   ```yaml
   projects:
     myproject:
       root: C:\Users\you\projects\myproject
       git_url: git@github.com:org/myproject.git
       remote: true   # optional: fleet + allowlisted file sync
   ```
   Optional sync allowlist in the repo: `<project>/.taskqueue/project.yaml` with `sync.push: [...]`.
   Then:
   ```powershell
   uv run tq sync
   ```

4. **Emit a join file** (contains `auth_token` — treat as a secret):
   ```powershell
   uv run tq worker config-master --host 100.64.0.1 --out worker-join.yaml
   ```
   Copy `worker-join.yaml` to the worker machine (USB, scp, etc.).

### B. On the worker machine

5. **Install taskqueue**
   ```powershell
   git clone git@github.com:wwylon/taskqueue.git
   cd taskqueue
   uv sync
   ```

6. **One-time setup from the join file**:
   ```powershell
   .venv\Scripts\python.exe -m taskqueue.cli worker setup `
     --from path\to\worker-join.yaml `
     --worker-id laptop-B
   ```
   This clones projects (when `git_url` is set) and writes `worker.yaml`.

   Manual alternative (no join file): `tq worker setup --master http://100.64.0.1:8765 --token <token> --worker-id laptop-B`.

7. **Start the worker and leave it running**
   ```powershell
   .venv\Scripts\python.exe -m taskqueue.cli worker
   ```
   (Auth comes from setup / `worker.yaml` / `TQ_AUTH_TOKEN`.)

   After big master edits, warm workers with: `tq resync <project> --worker laptop-B` (on the master).

### C. Check that it works

8. **On the master:**
   ```powershell
   uv run tq worker list
   ```
   Your worker should show **online**.

9. **Enqueue a remote task** under `myproject/experiments/operator_queue/`, for example:
   ```yaml
   task_id: hello-remote
   command: python
   args: ["-c", "print('hello'); print('(100.0%)')"]
   dispatch: remote
   priority: 10
   resources:
     cpu_cores: 1
     ram_gib: 1
   ```
   ```powershell
   uv run tq sync
   uv run tq diagnose myproject/hello-remote
   ```
   When it finishes, `worker=` should be `laptop-B` (not `local`).

If something fails, jump to [§9 Troubleshooting](#9-troubleshooting). Details for every file and flag are below.

---

## 1. Roles and homes

| Role | Machine | State home | What it does |
|------|---------|------------|--------------|
| **Master** | Development / overnight host | `~/.taskqueue` (default) | Runs daemon + HTTP API; admits local tasks; leases remote runs |
| **Worker** | Spare laptop/desktop | Own home (often default `~/.taskqueue` on that machine) | Pulls work from master URL; executes; streams logs; finalizes |

`TASKQUEUE_HOME` selects the state directory. Everything else (DB, `projects.yaml`, `worker.yaml`, logs, `daemon.pid`) lives under that home.

```
{TASKQUEUE_HOME}/
  projects.yaml      # project registry (master and worker both have one)
  worker.yaml        # worker only: master_url + worker_id (+ optional pin)
  queue.db           # master authoritative DB (workers do not share this)
  daemon.pid / daemon.log
  logs/<project>/
  bundles/<project>/
  packages/<project>/
  pending_finalize/  # worker: outbox if finalize HTTP fails
  cache/code/        # worker: content-addressed code packages
  runs/<run_id>/work # worker: per-attempt workspace
```

Workers initiate all HTTP. The master never opens outbound connections to workers.

---

## 2. Master URL and port

| Target | Master home | Port | Worker must use |
|--------|-------------|------|-----------------|
| Default overnight | `~/.taskqueue` | **8765** | `--master http://<master-ip>:8765` |

Confirm with `tq doctor` on the master (home + port) before starting a worker.

---

## 3. Master configuration

Run these on the **master** machine (default home; omit `TASKQUEUE_HOME`).

### 3.1 Start the daemon

```powershell
cd path\to\taskqueue
uv run tq start
uv run tq doctor
```

Confirm: `daemon: running`, expected `web_port`, `schema_version` matches code.

### 3.2 Bind beyond loopback (required for remote workers)

Default bind is `127.0.0.1` — only the master machine can reach the API. Remote workers need a reachable address:

```powershell
tq config set bind_host=0.0.0.0
# optional: confirm port
tq config set web_port=8765
tq restart
# or: tq stop-daemon && tq start
tq doctor   # auth_token should be on after next step
```

Non-loopback bind **refuses to start** if `auth_token` is empty.

### 3.3 Auth token (required when bind is not loopback)

```powershell
tq token generate
# prints token once; also stored in config as auth_token
```

Or set explicitly: `tq config set auth_token=<secret>`.

Workers and remote clients must send the same token:

- Env: `$env:TQ_AUTH_TOKEN = "<token>"`
- CLI: `tq worker … --token <token>`
- Header: `Authorization: Bearer <token>` or `X-TQ-Token: <token>`

UI (browser on LAN): `localStorage.setItem('tq_token', '<token>')`.

Scoped tokens created on the web **API** page (`tq_live_…`, SHA-256 at rest) are for operator HTTP calls only. Workers and the master lease still use this `auth_token`. See [Security](security.md) and [Web UI](operators/web-ui.md).

### 3.4 Register projects with `git_url`

Edit `{home}/projects.yaml` on the master:

```yaml
projects:
  myapp:
    root: C:\Users\you\projects\myapp
    queue_dir: experiments/operator_queue   # optional; this is the default
    git_url: https://github.com/example/myapp.git
  tqdemo:
    root: path\to\taskqueue\fixtures\tqdemo
    git_url: null   # fixture-only; worker cannot clone — use local path or skip
```

`git_url` is what `tq worker setup` clones onto the worker. Without it, setup still registers the project name but skips clone (operator must place code manually or use `code.provider: git_patch` / `bundle` from the master).

Then:

```powershell
tq sync
tq list --project <name>
```

### 3.5 Optional master knobs that affect remotes

| Config | Default | Effect |
|--------|---------|--------|
| `web_port` | 8765 | URL workers use |
| `bind_host` | `127.0.0.1` | Must be `0.0.0.0` (or Tailscale IP) for remotes |
| `auth_token` | empty | Required if bind ≠ loopback |
| `hung_task_k` | 5 | Hung remote detection (0 = off) |
| `notify_allow_remote` | 0 | Allow non-loopback `notify_url` hosts |
| `toast_notifications` | 0 | When `1`, end-of-task / start toasts use Windows Action Center (needs optional `winotify`; `pip install winotify` or `uv sync --extra toast`). Independent of per-alert **Windows popup** on the Alerts page (system alerts toast when that row is on). |

Lease TTL is fixed in code (`LEASE_TTL_SEC = 90`); workers renew via heartbeat / log-chunk.

### 3.6 Firewall / network

1. Prefer [Tailscale](https://tailscale.com/) on master and workers (same account).
2. Note master Tailscale IP (`100.x.y.z`).
3. From the worker: `ping 100.x.y.z` then `curl http://100.x.y.z:8765/api/status` with the Bearer token if required.
4. Windows Firewall: allow inbound TCP on the daemon port if not using Tailscale-only routing.

Do **not** expose the scheduler on the public Internet without Tailscale (or equivalent private mesh).

---

## 4. Worker configuration

### 4.1 Prerequisites on the worker machine

1. Python **3.12+** recommended (3.10+ minimum) and [`uv`](https://github.com/astral-sh/uv).
2. Git with SSH keys (or HTTPS) that can clone the same repos as `git_url`.
3. GPU drivers if claiming GPU tasks.
4. Clone of **taskqueue**:

```powershell
git clone git@github.com:wwylon/taskqueue.git
cd taskqueue
uv sync
```

### 4.2 Automated setup (recommended)

From the taskqueue checkout on the **worker**:

```powershell
# Point at the master
$env:TQ_AUTH_TOKEN = "<token-from-master>"   # if auth enabled

.venv\Scripts\python.exe -m taskqueue.cli worker setup `
  --master http://100.64.0.1:8765 `
  --worker-id laptop-B `
  --token $env:TQ_AUTH_TOKEN
```

What setup does:

1. `GET /api/worker/projects` on the master.
2. For each project with `git_url`, clones into a local root (Windows: `~/projects/<name>` if `D:\` exists, else `~/projects/<name>`; under a non-default home, `{home}/projects/<name>`).
3. Runs `uv sync` when `pyproject.toml` is present.
4. Writes `{home}/projects.yaml` and `{home}/worker.yaml`.

Default worker home is `~/.taskqueue` on that machine unless `TASKQUEUE_HOME` is set. Use a **separate** home on the worker if you need isolated dogfood state; the worker’s home is not the master’s DB.

### 4.3 `worker.yaml` (saved by setup)

Path: `{TASKQUEUE_HOME}/worker.yaml`

```yaml
master_url: http://100.64.0.1:8765
worker_id: laptop-B
# pin_worker_version: false   # optional; if true, skip auto wheel upgrade from master
```

| Field | Required | Meaning |
|-------|----------|---------|
| `master_url` | yes | Base URL of the master HTTP API (no trailing path) |
| `worker_id` | yes | Stable id (`[A-Za-z0-9_.-]{1,64}`); defaults to hostname if omitted at setup |
| `pin_worker_version` | no | When true, worker does not auto-upgrade from master’s wheel endpoint |

CLI flags override the file for one run:

```powershell
tq worker --master http://100.64.0.1:8765 --worker-id worker-B --token <token>
```

### 4.4 Worker `projects.yaml`

Written by setup. Lists projects this agent may register for. The **master** also filters claims by the `projects` list the worker advertises at register time — the worker must list a project to claim its tasks.

```yaml
projects:
  myapp:
    root: C:\Users\you\projects\myapp
    git_url: https://github.com/example/myapp.git
```

Re-run `tq worker setup` or edit this file after adding projects on the master, then restart the worker (or use `tq worker sync <worker_id>` from the master to request a git sync on next heartbeat).

### 4.5 Start the worker agent

```powershell
cd D:\path\to\taskqueue
$env:TQ_AUTH_TOKEN = "<token>"   # if required

.venv\Scripts\python.exe -m taskqueue.cli worker
# or: uv run tq worker
```

Expected stderr/stdout:

```
[worker] Starting worker 'laptop-B'...
[worker] System Info: OS=windows, Arch=amd64, RAM=31.8G, Cores=12, VRAM=0.0G
[worker] Master Coordinator: http://100.64.0.1:8765
```

The agent long-polls `GET /api/worker/next-task?wait=…` (up to 30s) when idle.

Keep the process running (Task Scheduler / systemd / `nssm` / open terminal). One agent per machine is enough for multi-slot concurrency.

### 4.6 Auth on the worker

| Method | Example |
|--------|---------|
| Environment | `$env:TQ_AUTH_TOKEN = "…"` |
| Flag | `tq worker --token …` / `tq worker setup --token …` |
| Missing token | Register/claim fail with 401 when master has `auth_token` set |

---

## 5. Verify end-to-end

### On the master

```powershell
tq doctor
tq worker list          # Status = online, Last Seen recent
tq capacity             # worker row with free CPU/RAM/GPU
```

Web UI → **Workers** tab (prod `http://127.0.0.1:8765` or Tailscale IP; use token in UI if auth is on).

### Claim a test task

Enqueue a small remote-capable YAML (or retry a fixture on dev):

```yaml
task_id: hello-remote
command: python
args: ["-c", "print('hello'); print('(100.0%)')"]
dispatch: remote
priority: 10
resources:
  cpu_cores: 1
  ram_gib: 1
```

```powershell
# write YAML under <project>/experiments/operator_queue/
tq sync
tq list --project <project>
# wait or watch UI
tq diagnose <project>/hello-remote   # last_run worker= should be the worker_id
```

If `dispatch: remote` and **no** workers are online, the master runs the task **locally** (prefer-remote hint). Confirm `tq worker list` shows online before overnight remote work.

Uneven fleets: declare accurate `resources.ram_gib` / VRAM and keep heartbeats healthy — default `placement_mode=binpack` prefers the roomier online worker. See [master_worker.md](master_worker.md) “Many workers”.

### Logs

Remote logs stream to the master during the run (`POST /api/worker/log-chunk`). Use `tq logs <project/task_id>` or the UI before finalize completes.

---

## 6. Task YAML knobs that matter for remotes

| Field | Why |
|-------|-----|
| `dispatch: local \| remote \| any` | Where the task may run ([master_worker.md](master_worker.md)). For projects with `remote: true`, **omitted** dispatch defaults to `any` (fleet). Explicit `local` still pins master. |
| `requires` | OS / min RAM / GPU / tags — must match worker advertise |
| `resources` | Declared cost for remote packing; set `ram_gib` near peak; GPU VRAM keeps tasks off GPU-less workers |
| `code.provider` | `git_patch` (default in git repos), `bundle`, or `project_root` |
| `bundle` / `bundle_hash` | Pin `tq bundle` artifact |
| `transfer` | Push/pull files (≤64 MiB upload); not multi-GB |
| Allowlisted file sync | `<project>/.taskqueue/project.yaml` `sync.push` + `tq resync` |
| `execution_semantics` | Lease loss → re-queue vs `unknown` |
| `resume_args` | Resume after interrupt when last run was interrupted |

Full field reference: [task_standards.md](task_standards.md).

---

## 7. Day-2 operations

| Action | Command / note |
|--------|----------------|
| List workers | `tq worker list` (master) |
| Remove stale worker row | `tq worker remove <worker_id>` |
| Ask worker to git sync | `tq worker sync <worker_id>` (picked up on next heartbeat) |
| Build wheel for workers | `tq worker build-wheel` (master; optional auto-upgrade path) |
| Restart worker after master upgrade | Stop agent → pull matching taskqueue version → start |
| Pending finalize after crash | Restart worker; `{home}/pending_finalize/` flushes before new claims |
| Point worker at another master | Edit `worker.yaml` `master_url` or pass `--master`; restart |

---

## 8. Configuration checklist

**Master**

- [ ] Correct `TASKQUEUE_HOME` (`tq doctor`)
- [ ] Daemon running; port matches URL workers will use
- [ ] `bind_host=0.0.0.0` (or Tailscale IP) if remotes exist
- [ ] `auth_token` set; token shared only with workers / operators
- [ ] `projects.yaml` entries have `root` + `git_url` where clone is desired
- [ ] `tq sync` succeeds; overnight YAMLs use deliberate `dispatch`

**Worker**

- [ ] Can reach `http://<master>:<port>/api/status` (with token if needed)
- [ ] `worker.yaml` has correct `master_url` and unique `worker_id`
- [ ] `TQ_AUTH_TOKEN` / `--token` matches master
- [ ] `projects.yaml` lists projects this machine should run
- [ ] Agent process kept alive; appears **online** in `tq worker list`

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Worker cannot connect | Wrong IP/port; firewall; bind still loopback | `tq doctor` on master; ping/curl from worker; `bind_host` |
| 401 / auth errors | Token mismatch | Regenerate or copy `auth_token`; set `TQ_AUTH_TOKEN` |
| **503 / `not master`** | Master `master_lease` expired or another daemon holds it | `tq doctor` — if lease shows **(expired)**, `tq restart` (or `stop-daemon` then `start`); confirm positive TTL. Lease renews on a background thread (TTL 600s). Clean shutdown releases the lease; same-host restart also reclaims when the old PID is dead. |
| Register/heartbeat timeout | Master overloaded / SQLite busy; client wait exceeded | Retry; avoid huge `tq sync` storms while attaching; worker POST timeout is 30s |
| Daemon won’t start on `0.0.0.0` | Empty `auth_token` | `tq token generate` |
| Worker online but never claims | Project not in worker’s projects list; `requires` mismatch; `dispatch: local` | Check register projects; `tq diagnose`; YAML `dispatch`/`requires` |
| UI says online, worker fails | Stored `status=online` until 180s offline sweep; check **health** (unreachable) on Workers tab | Restart worker; fix 503/timeouts above |
| `dispatch: remote` runs on master | Zero online workers | Start worker; confirm `tq worker list` |
| Task `unknown` | Lease lost + non-idempotent semantics | `tq retry`; set `execution_semantics` deliberately |
| Setup clone fails | Missing/invalid `git_url` or SSH keys | Fix master’s `projects.yaml`; test `git clone` on worker |
| Schema / version drain | Worker older than master | Upgrade worker checkout; optional wheel upgrade |

More architecture detail: [master_worker.md](master_worker.md) triage section.

---

## 10. CLI quick reference

```
# Master
tq start / tq stop-daemon / tq restart
tq doctor
tq config get | set k=v
tq token generate
tq sync
tq worker list
tq worker remove <id>
tq worker sync <id>
tq capacity
tq diagnose <project/task_id>

# Worker machine
tq worker setup --master <url> [--worker-id <id>] [--token T]
tq worker --master <url> --worker-id <id> [--token T]
tq worker          # uses {home}/worker.yaml
```

Environment variables:

| Variable | Used by | Meaning |
|----------|---------|---------|
| `TASKQUEUE_HOME` | master + worker CLI | State directory |
| `TQ_AUTH_TOKEN` | worker / HTTP clients | Bearer token |
| `TQ_MASTER` | CLI/MCP mutations | Optional remote master URL (see [mcp.md](mcp.md)) |
| `TQ_OPERATOR` | CLI / MCP | Attribution for `started_by` |
