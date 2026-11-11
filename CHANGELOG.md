# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.0] - 2026-11-11

### Changed

- **Renamed the project from `taskqueue` to `tqdaemon`** (PyPI distribution `tqdaemon`; `taskqueue`, `tq` and `task-queue` were taken). The `tq` command is unchanged; the Python package is now `tqdaemon`.
- New names: `TQDAEMON_HOME` (env), `~/.tqdaemon` (state home), `.tqdaemon/project.yaml` (per-project config), MCP server name `tqdaemon`.
- Migration: the old env var `TASKQUEUE_HOME`, an existing `~/.taskqueue` home, `.taskqueue/project.yaml` and runs started under the old wrapper module are still recognised. To migrate by hand: stop the daemon, rename `~/.taskqueue` to `~/.tqdaemon`, reinstall (`pip install -U tqdaemon`), `tq start`.

### Added

- Automated demo GIF (`scripts/record_demo.py`), issue/PR templates and Dependabot config; PyPI publishing wired into the house publish flow.

## [0.7.6] - 2026-11-08

### Added

- Slim stranger-facing README (EN/PL/ZH/AR) with hero image, agent CTA, and transparency note (no product phone-home).
- Marketing GitHub Pages tree under `site/` (landing, sitemap, robots); CLI encyclopedia moved to `docs/operators/cli.md`.
- Root `llms.txt` and expanded package keywords for discoverability.

### Fixed

- Deep-audit reliability work for remote `stopped` claim paths, MCP mutate helpers, scheduler admit hot path, and web UI SSE/log races (F-244–F-258).

## [0.7.5] - 2026-11-01

### Added

- Queue hygiene report: `tq hygiene` / `tq report --hygiene`, MCP `report` with `kind=hygiene`, and `GET /api/hygiene` — churn rates, multi-use vs flaky classification, and honest archive proposals (`agent_prompt`; never auto-archives).
- `tq archive --max-completed-runs N` so archive proposals can skip multi-use tasks (hygiene uses `1`).
- Tasks keep `completed_run_count` / `total_run_count` (schema 33); `tq list` reads them instead of scanning all runs.
- History tab pages with a fixed offset (Prev/Next) and mounts log/history expand rows only when opened.
- Help topic `queue-hygiene` and operator/docs notes distinguishing hygiene from retention prune and agent usage summaries.
- House `publish.manifest.yaml` and dual-tree export readiness for the public GitHub mirror.

### Changed

- Unscoped queue `sort=deps` / `sort=order` require a project filter when more than 2000 tasks match.
- `/api/deps` applies project, suite, and `project/task_id` focus in SQL before building the graph; unscoped graphs cap the SELECT at 500 nodes.
- `GET /api/tasks/<project>/<task_id>/runs` is paginated (`limit` default 50, max 200; `offset`).
- Unscoped `GET /api/task-stats` returns at most 500 tasks (prefer `project=`).
- History anomaly badges are computed in a batched pass per page instead of one query per completed run.

## [0.7.4] - 2026-10-26

### Added

- Initial public changelog baseline matching `pyproject.toml` version 0.7.4.
