---
id: page-schedule
title: Schedule
summary: Tasks that already have schedule.cron.
ui_pages: [schedule]
audience: [human]
guide_anchors:
  web-ui: page-schedule
---

**Schedule** lists tasks whose YAML contains `schedule.cron` (five fields: minute, hour, day of month, month, day of week, evaluated in UTC). **Next fire** is the next matching minute within 48 hours.

- **Run now** re-queues the task.
- **Edit** opens that task's YAML.
- **Pause** sets `enabled: false`; a paused row shows **Resume** instead, which sets it back to `enabled: true`.
- **New schedule** does not insert a task. Put `schedule.cron` in the project queue YAML and run `tq sync`. If a row is selected, the button opens that YAML. See the `schedule` field in [task standards](/docs/task-standards).
