# AGENTS.md — taskqueue (public)

Bootstrap for agents working on the **public** taskqueue tree.

1. [README.md](README.md) — install, CLI, quickstart  
2. [docs/task_standards.md](docs/task_standards.md) — YAML fields  
3. [docs/mcp.md](docs/mcp.md) — MCP tools  
4. [docs/WORKER_SETUP.md](docs/WORKER_SETUP.md) — remote workers  
5. [CONTRIBUTING.md](CONTRIBUTING.md) — tests and PR expectations  

## Hard constraints

- Long jobs → operator-queue YAML (not in-session shells).
- **Create:** MCP `submit_task` (preferred) or write YAML under the project's `experiments/operator_queue/` + `tq sync`. Do **not** use `enqueue_task` to create — it only retries terminal tasks. See [docs/agents/recipes.md](docs/agents/recipes.md) and [docs/mcp.md](docs/mcp.md).
- Do not invent CLI commands or YAML keys; use `docs/task_standards.md`.
- Prefer `tq wait` / MCP `wait_for_task` over sleep polling.
- Set `TQ_OPERATOR` so `started_by` / `my_tasks` work.

## Tests

```text
uv sync --group dev
uv run pytest -q -m "not stress"
```
