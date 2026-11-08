# taskqueue

[English](README.md) | [Polski](README.pl.md) | [中文](README.zh.md) | [العربية](README.ar.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![GitHub stars](https://img.shields.io/github/stars/wwylon/taskqueue?style=social)](https://github.com/wwylon/taskqueue)

![taskqueue — 本地任务、代理、worker](docs/assets/hero.jpg)

**taskqueue** 在你自己的机器上跑长任务，并按资源限流，避免把机器打满。
用 YAML 描述任务；本地 **daemon** 仅在 CPU / RAM / GPU 显存足够时启动作业。
可通过 **网页 UI**、**`tq` CLI** 或 **AI 编程代理（MCP）** 查看进度。

不是 Celery、Kubernetes 或 CI。也不是 2011 年的 AMQP/Debian
[rojkov/taskqueue](https://github.com/rojkov/taskqueue)（simpletaskqueue）。

## 为什么用

- 按资源准入（CPU / RAM / GPU），本机或远程 worker
- YAML：检查点、依赖、优先级、稳定错误码
- 面向代理：通过 MCP 提交、等待、诊断
- 本地优先 — 无需云 SaaS 账号

## 最佳起点：让代理接入

让 Cursor / Claude Code 等代理用 **taskqueue** 跑长任务：配置 MCP（`tq mcp`），
提交 YAML，用 `wait_for_task`，而不是卡住对话。说明：**[docs/mcp.md](docs/mcp.md)**。

## 安装

**需要：** Python **3.12+**、[Git](https://git-scm.com/)、[`uv`](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/wwylon/taskqueue.git
cd taskqueue
uv sync
uv run tq doctor
uv run tq start
```

打开 **http://127.0.0.1:8765**。示例见
[`examples/minimal`](examples/minimal/)。

## 文档

| 对象 | 入口 |
|------|------|
| 人类 | [docs/README.md](docs/README.md) · [Quickstart](docs/operators/quickstart.md) · [Web UI](docs/operators/web-ui.md) · [CLI](docs/operators/cli.md) |
| 代理 | [AGENTS.md](AGENTS.md) · [MCP](docs/mcp.md) · [Cookbook](docs/agents/task-script-cookbook.md) · [llms.txt](llms.txt) |

**进阶：** [Security](docs/security.md) · [Task standards](docs/task_standards.md) · [Scheduler](docs/scheduler.md) · [Workers](docs/WORKER_SETUP.md)

## 透明度

我们不主动上报使用数据。公开信号是 GitHub star；PyPI 发布后可用公开下载量。
详见 [docs/security.md](docs/security.md#privacy--telemetry)。

## 许可

MIT — [LICENSE](LICENSE) · [Changelog](CHANGELOG.md)
