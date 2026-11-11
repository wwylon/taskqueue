# tqdaemon

[English](README.md) | [Polski](README.pl.md) | [中文](README.zh.md) | [العربية](README.ar.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/wwylon/tqdaemon/actions/workflows/ci.yml/badge.svg)](https://github.com/wwylon/tqdaemon/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tqdaemon)](https://pypi.org/project/tqdaemon/)
[![Latest tag](https://img.shields.io/github/v/tag/wwylon/tqdaemon?label=version)](https://github.com/wwylon/tqdaemon/tags)

![tqdaemon — مهام محلية ووكلاء وعمال](https://raw.githubusercontent.com/wwylon/tqdaemon/main/docs/assets/demo.gif)

**tqdaemon** يشغّل المهام الطويلة على أجهزتك دون إنهاكها.
تصف كل مهمة في YAML. يعمل **daemon** محلي ويبدأ العمل فقط عند توفر
CPU وRAM وذاكرة GPU كافية. راقب التقدّم عبر **واجهة ويب** أو CLI **`tq`**
أو وكيل ترميز بالذكاء الاصطناعي (**MCP**).

ليس Celery أو Kubernetes أو CI. وليس مشروع AMQP/Debian القديم من 2011
[rojkov/taskqueue](https://github.com/rojkov/taskqueue) (simpletaskqueue).

## لماذا

- قبول المهام حسب الموارد (CPU / RAM / GPU) محلياً أو على عمال بعُدين
- YAML مع نقاط حفظ وتبعيات وأولويات ورموز أخطاء مستقرة
- مخصّص للوكلاء: إرسال وانتظار وتشخيص عبر MCP
- محلي أولاً — بلا حساب SaaS سحابي

## أفضل بداية: اطلب من الوكيل

اطلب من وكيلك (Cursor أو Claude Code وغيرهما) استخدام **tqdaemon** للمهام
الطويلة: إعداد MCP (`tq mcp`) وإرسال YAML و`wait_for_task` بدل حجب المحادثة.
الإعداد: **[docs/mcp.md](docs/mcp.md)**.

## التثبيت

**المتطلبات:** Python **3.12+** و[Git](https://git-scm.com/) و[`uv`](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/wwylon/tqdaemon.git
cd tqdaemon
uv sync
uv run tq doctor
uv run tq start
```

افتح **http://127.0.0.1:8765**. مثال:
[`examples/minimal`](examples/minimal/).

## التوثيق

| الجمهور | ابدأ من |
|---------|---------|
| بشر | [docs/README.md](docs/README.md) · [Quickstart](docs/operators/quickstart.md) · [Web UI](docs/operators/web-ui.md) · [CLI](docs/operators/cli.md) |
| وكلاء | [AGENTS.md](AGENTS.md) · [MCP](docs/mcp.md) · [Cookbook](docs/agents/task-script-cookbook.md) · [llms.txt](llms.txt) |

**متقدم:** [Security](docs/security.md) · [Task standards](docs/task_standards.md) · [Scheduler](docs/scheduler.md) · [Workers](docs/WORKER_SETUP.md)

## الشفافية

لا نرسل بيانات استخدام خفية. الإشارات العامة: نجوم GitHub؛ وبعد نشر PyPI
عدادات التحميل العامة. التفاصيل: [docs/security.md](docs/security.md#privacy--telemetry).

## الرخصة

MIT — [LICENSE](LICENSE) · [Changelog](CHANGELOG.md)
