# Code ownership

Keep changes with the smallest owning surface:

| Surface | Primary owner |
|---|---|
| Scheduler and admission | `tqdaemon/daemon.py`, `tqdaemon/scheduler.py`, `tqdaemon/resources.py` |
| HTTP API and auth | `tqdaemon/web/`, `tqdaemon/auth.py` |
| Static web UI | `tqdaemon/static/` |
| Documentation registry and generated help | `docs/manifest.json`, `docs/help/`, `scripts/build_docs.py` |
| Persistence and queries | `tqdaemon/db.py`, `tqdaemon/_db_*.py`, `tqdaemon/queries.py` |

Prefer a focused helper or route change over a package-wide rewrite. Generated
documentation artifacts must be refreshed with `scripts/build_docs.py`.
