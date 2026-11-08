# Documentation hub

Single registry: [`manifest.json`](manifest.json). Help topics live in [`help/`](help/) and are reused by the web UI **?** modal, **Docs** tab, and guides below.

## Humans

| Entry | Path |
|-------|------|
| Quickstart | [operators/quickstart.md](operators/quickstart.md) |
| Web UI tour | [operators/web-ui.md](operators/web-ui.md) |
| CLI reference | [operators/cli.md](operators/cli.md) |

Root README translations: [EN](../README.md) · [PL](../README.pl.md) · [ZH](../README.zh.md) · [AR](../README.ar.md). When the English pitch changes, update siblings in the same change or remove a stale sibling.

## Agents

| Entry | Path |
|-------|------|
| Bootstrap | [../AGENTS.md](../AGENTS.md) |
| **Task script cookbook** | [agents/task-script-cookbook.md](agents/task-script-cookbook.md) |
| Recipes | [agents/recipes.md](agents/recipes.md) |
| MCP | [mcp.md](mcp.md) |

## Shared reference

| Topic | Path |
|-------|------|
| YAML fields (themed) | [task_standards.md](task_standards.md) |
| Task clients (`tq_client`) | [../clients/README.md](../clients/README.md) |
| Examples | [../examples/](../examples/) |
| Scheduler | [scheduler.md](scheduler.md) |
| Error codes | [errors.md](errors.md) |
| Security | [security.md](security.md) |
| Workers | [WORKER_SETUP.md](WORKER_SETUP.md) |

## HTTP API

In-app: `/api/docs` · Machine-readable: `/api/docs.json`

**Authoring:** edit `docs/help/{id}.md` (**English only**). Do not add translated topic files or update `help.*Body` in pl/zh/ar locales when changing help — those locale strings are legacy UI fallback; new page-specific topics (`page-*`) appear in English only. Run `uv run python scripts/build_docs.py` after changes.
