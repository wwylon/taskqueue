# taskqueue

[English](README.md) | [Polski](README.pl.md) | [中文](README.zh.md) | [العربية](README.ar.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![GitHub stars](https://img.shields.io/github/stars/wwylon/taskqueue?style=social)](https://github.com/wwylon/taskqueue)

![taskqueue — local jobs, agents, workers](docs/assets/hero.jpg)

**taskqueue** runs long jobs on your own machines without melting them.
Describe each job in YAML. A local **daemon** starts work only when there is
enough CPU, RAM, and GPU memory. Watch progress in a **web UI**, the **`tq`**
CLI, or from an **AI coding agent** (MCP).

Not Celery, Kubernetes, or CI. Not the 2011 AMQP/Debian
[rojkov/taskqueue](https://github.com/rojkov/taskqueue) (simpletaskqueue).

## Why use it

- Resource-aware admission (CPU / RAM / GPU VRAM) on one PC or with remote workers
- YAML jobs with checkpoints, deps, priorities, and stable error codes
- Built for **agents**: submit, wait, diagnose over MCP
- Local-first — no cloud SaaS account

## Best start: ask your agent

Tell your coding agent (Cursor, Claude Code, …) to use **taskqueue** for long
jobs: wire MCP (`tq mcp`), submit YAML tasks, and `wait_for_task` instead of
blocking the chat. Setup: **[docs/mcp.md](docs/mcp.md)**.

## Install

**Need:** Python **3.12+**, [Git](https://git-scm.com/), [`uv`](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/wwylon/taskqueue.git
cd taskqueue
uv sync
uv run tq doctor
uv run tq start
```

Open **http://127.0.0.1:8765**. Try the sample in
[`examples/minimal`](examples/minimal/) (register it as a project, then
`tq sync` / enable / `tq wait` — see that folder’s README).

Optional Windows toasts: `uv sync --extra toast`. Autostart:
`scripts/install_tq_autostart.ps1`.

## Docs

| Audience | Start here |
|----------|------------|
| Humans | [docs/README.md](docs/README.md) · [Quickstart](docs/operators/quickstart.md) · [Web UI](docs/operators/web-ui.md) · [CLI](docs/operators/cli.md) |
| Agents | [AGENTS.md](AGENTS.md) · [MCP](docs/mcp.md) · [Cookbook](docs/agents/task-script-cookbook.md) · [llms.txt](llms.txt) |

**Advanced:** [Security & privacy](docs/security.md) · [Task standards](docs/task_standards.md) · [Scheduler](docs/scheduler.md) · [Workers](docs/WORKER_SETUP.md)

## Transparency

We do **not** phone home. Public signals are GitHub stars (badge above) and,
after a PyPI release under a distinct package name, download counts you can
verify yourself. Maintainers may use GitHub Traffic Insights (clones ≠ installs).
Details: [docs/security.md](docs/security.md#privacy--telemetry).

## License

MIT — see [LICENSE](LICENSE) · [Changelog](CHANGELOG.md)
