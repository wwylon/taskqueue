# CLAUDE.md — tqdaemon (public)

Public checkout guidance. Prefer [AGENTS.md](AGENTS.md) and [docs/](docs/).

- Package: `tqdaemon/` (`cli.py`, `daemon.py`, `db.py`, `worker.py`, …)
- Web UI: `tqdaemon/static/` + ES modules under `tqdaemon/static/js/`
- Scheduler tick: `daemon.daemon_tick` — see `tests/test_daemon_tick.py`
- Docs hub: [docs/README.md](docs/README.md)

Do not assume a sibling `devkit` or shared inbox layout; this repository is standalone for public users.
