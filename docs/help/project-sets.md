---
id: project-sets
title: Project sets
summary: Multilevel project-set tree for scoping, multi-select, bulk ops, and inherited limits.
ui_pages: [queue, config]
audience: [human, agent]
guide_anchors:
  web-ui: project-sets
---

A **project set** is an optional slash-path label on a registered project in `projects.yaml` (for example `games/card` or `med/imaging`). Sets form a tree: selecting a parent includes every project under that path and its descendants.

Use sets as the **primary Queue/History navigator**, to multi-check projects for bulk freeze/pause/stop, and to set **inherited limit defaults** (admission caps) that members pick up unless overridden.

Different from task YAML `group:` (the Groups page / `tq group`) — that labels **tasks** inside a project. Project sets label **projects**.

## Registry

```yaml
projects:
  pokerbot:
    root: /path/to/pokerbot
    set: games/card
```

## Limits inheritance

Each set path may store the same six fields as a project (`max_tasks`, `min_tasks`, `max_cpu_cores`, `max_ram_gib`, `max_gpu_vram_gib`, `max_gpu_hours_day`). For each field the scheduler uses:

1. The project's own override if non-zero
2. Else the nearest ancestor set default (exact path, then parents)
3. Else unlimited (`0`)

Membership changes apply immediately — there is no copy of set defaults onto projects.

## CLI

- `tq set tree` — print the tree
- `tq set show <path>` — subtree members
- `tq set assign <project> <path>` / `tq set clear <project>`
- `tq set rename <old> <new>` / `tq set move <old> <new>`
- `tq set limits <path> field=val…` / `tq set limits-show <path>`
- `tq set freeze|unfreeze|pause|resume|stop <path>` — subtree bulk actions

## Web UI

- **Set tree** (above project chips): scopes Queue/History to a subtree (`?set=`).
- **Project chips**: multi-check within the scope (`?projects=a,b` or `?project=` when one). Empty checks = all members of the set.
- **Bulk toolbar**: Freeze / Pause / Stop all for the checked projects, or the whole set when none are checked.
- **Config → Projects & sets**: edit set-level defaults and per-project overrides (with inheritance labels). **Set membership** assigns/renames paths.
