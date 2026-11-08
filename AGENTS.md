# AGENTS.md — taskqueue (public)

Bootstrap for agents on the **public** taskqueue tree.

1. [README.md](README.md) — pitch, install, first win  
2. [llms.txt](llms.txt) — compact agent index  
3. [docs/mcp.md](docs/mcp.md) — MCP setup + tools  
4. [docs/task_standards.md](docs/task_standards.md) — YAML fields  
5. [docs/agents/task-script-cookbook.md](docs/agents/task-script-cookbook.md) — long jobs with `tq_client`  
6. [docs/operators/cli.md](docs/operators/cli.md) — full `tq` reference  
7. [CONTRIBUTING.md](CONTRIBUTING.md) — tests and PRs  

## Hard constraints

- Long jobs → operator-queue YAML (not in-session shells).
- **Create:** MCP `submit_task` (preferred) or YAML under `experiments/operator_queue/` + `tq sync`. `enqueue_task` only retries terminal tasks.
- Do not invent CLI commands or YAML keys.
- Prefer `tq wait` / MCP `wait_for_task` over sleep polling.
- Set `TQ_OPERATOR` so `started_by` / `my_tasks` work.
- Default home `~/.taskqueue`, UI **8765**.

## Tests

```text
uv sync --group dev
uv run pytest -q -m "not stress"
```
