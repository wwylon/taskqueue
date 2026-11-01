# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
