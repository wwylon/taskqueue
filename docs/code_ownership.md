# Code ownership

Keep changes with the smallest owning surface:

| Surface | Primary owner |
|---|---|
| Scheduler and admission | `taskqueue/daemon.py`, `taskqueue/scheduler.py`, `taskqueue/resources.py` |
| HTTP API and auth | `taskqueue/web/`, `taskqueue/auth.py` |
| Static web UI | `taskqueue/static/` |
| Documentation registry and generated help | `docs/manifest.json`, `docs/help/`, `scripts/build_docs.py` |
| Persistence and queries | `taskqueue/db.py`, `taskqueue/_db_*.py`, `taskqueue/queries.py` |

Prefer a focused helper or route change over a package-wide rewrite. Generated
documentation artifacts must be refreshed with `scripts/build_docs.py`.
