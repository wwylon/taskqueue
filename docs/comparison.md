# tqdaemon compared with other tools

tqdaemon is a **local, resource-aware job scheduler for long-running commands**. You describe each job in
YAML; a daemon on your own machine starts it only when enough CPU, RAM and GPU memory are free, keeps progress,
logs and checkpoints, and exposes everything through a web UI, the `tq` CLI and an MCP server for AI coding
agents. It is aimed at one desktop or workstation (optionally with a few remote workers), not at a shared
cluster or a production service.

## At a glance

| You need | Good fit | Why not tqdaemon |
|---|---|---|
| Run jobs at fixed times | cron, Windows Task Scheduler | They trigger on a clock. They do not wait for free RAM/VRAM, queue, prioritise, or show progress. |
| Background work for a web app (Python functions) | Celery, RQ, Dramatiq | They run your *Python code* through a broker (Redis, RabbitMQ). tqdaemon runs *any command* from YAML and needs no broker. |
| A shared multi-user HPC cluster | Slurm, PBS, LSF | Cluster schedulers with accounts, partitions and fair-share across many nodes. tqdaemon needs no admin and no cluster, but is not a multi-tenant system. |
| Containers on a cluster | Kubernetes Jobs, Nomad | Orchestrate containers across nodes. tqdaemon starts plain processes on machines you already own. |
| Scale one Python program across cores/nodes | Ray, Dask | Frameworks you write your code against. tqdaemon schedules whole programs you already have. |
| A scheduled data pipeline with a DAG, retries and backfills | Airflow, Prefect, Dagster, Snakemake | Workflow engines. tqdaemon has task dependencies, but its core job is *admitting* work only when the machine can take it; it does not model data lineage or backfills. |
| Run tests/builds on every commit | GitHub Actions and other CI | CI is triggered by repository events on hosted or self-hosted runners. tqdaemon runs long local jobs, including ones an agent submitted. |

## What tqdaemon is good at

- **Not melting your machine:** admission checks CPU, RAM and GPU VRAM before a task starts, so
  several long jobs can share one PC.
- **Jobs you already have:** any command, described in YAML, with priorities, dependencies, wall-clock caps,
  stable error codes, resume/checkpoint hooks and a progress contract (`(42.0%)` in output or the Python/C++
  clients).
- **Agents as first-class users:** submit, wait and diagnose over MCP, so a coding agent can start a long job and
  `wait_for_task` instead of blocking a chat.
- **Local-first:** state lives in a local SQLite database under `~/.tqdaemon`; no cloud account and no
  telemetry. Remote workers are optional ([master/worker](master_worker.md)).

## When not to use it

- Many users sharing one scheduler, quotas, accounting or strong isolation between users
  (see [security](security.md): it is not a sandbox or a security boundary).
- Latency-sensitive production workloads or services that must survive a machine loss.
- Large clusters (tens of nodes and up), or jobs that must be containerised and scheduled across a fleet.
- Time-triggered schedules as the main need; use cron or Task Scheduler (they can also call `tq`).

They combine well: cron or a CI job can submit work to tqdaemon, and an Airflow/Snakemake step can call `tq`
for the heavy command so the machine is not oversubscribed.
