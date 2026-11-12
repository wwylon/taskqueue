"""Aggregate (subtree) caps for project sets.

A limit on a set node — e.g. ``bots`` with ``max_tasks=4`` — caps the *total* over every
project in that subtree (``bots/...``), not each member separately. The root ("all") is the
global config (``max_running_tasks``, ``max_cpu_cores``, ``max_ram_gib``, ``max_gpu_vram_gib``).
Per-project overrides (``project_limit_*``) remain per-project caps.

Usage per tick/claim::

    checker = SetCapChecker(all_set_limits, set_paths, gpu_hours_today=lambda p: ...)
    reason = checker.check(project, running_by_proj, cpu_by_proj, ram_by_proj, gpu_by_proj,
                           cpu=..., ram=..., gpu=...)

Leaf module (no tqdaemon imports at module level).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

# Fields of a set node that are totals over its subtree. ``min_tasks`` stays a per-project
# floor that members inherit (see project_limits.resolve_effective_limits).
SET_AGGREGATE_FIELDS = ("max_tasks", "max_cpu_cores", "max_ram_gib", "max_gpu_vram_gib", "max_gpu_hours_day")


def _prefixes(set_path: str | None) -> list[str]:
    if not set_path:
        return []
    parts = set_path.split("/")
    return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]


class SetCapChecker:
    """Checks a candidate against every ancestor set's subtree totals."""

    def __init__(
        self,
        all_set_limits: Mapping[str, Mapping[str, float]] | None,
        set_paths: Mapping[str, str | None] | None,
        *,
        gpu_hours_today: Callable[[str], float] | None = None,
    ) -> None:
        self.caps: dict[str, dict[str, float]] = {}
        for path, lim in (all_set_limits or {}).items():
            vals = {f: float(lim.get(f, 0) or 0) for f in SET_AGGREGATE_FIELDS}
            if any(v > 0 for v in vals.values()):
                self.caps[path] = vals
        self.set_paths = dict(set_paths or {})
        self._gpu_hours_today = gpu_hours_today
        self._gpu_hours_cache: dict[str, float] = {}

    def __bool__(self) -> bool:  # nothing to enforce -> callers can skip the work
        return bool(self.caps)

    def members(self, path: str) -> list[str]:
        """Projects whose set path is ``path`` or below it."""
        pre = path + "/"
        return [p for p, sp in self.set_paths.items() if sp and (sp == path or sp.startswith(pre))]

    def capped_ancestors(self, project: str) -> list[str]:
        return [p for p in _prefixes(self.set_paths.get(project)) if p in self.caps]

    def usage(
        self,
        path: str,
        running_by_proj: Mapping[str, int],
        cpu_by_proj: Mapping[str, float],
        ram_by_proj: Mapping[str, float],
        gpu_by_proj: Mapping[str, float],
    ) -> dict[str, float]:
        members = self.members(path)
        return {
            "running": float(sum(running_by_proj.get(p, 0) for p in members)),
            "cpu": float(sum(cpu_by_proj.get(p, 0.0) for p in members)),
            "ram": float(sum(ram_by_proj.get(p, 0.0) for p in members)),
            "gpu": float(sum(gpu_by_proj.get(p, 0.0) for p in members)),
        }

    def gpu_hours(self, path: str) -> float:
        if path not in self._gpu_hours_cache:
            fn = self._gpu_hours_today
            self._gpu_hours_cache[path] = float(sum(fn(p) for p in self.members(path))) if fn else 0.0
        return self._gpu_hours_cache[path]

    def check(
        self,
        project: str,
        running_by_proj: Mapping[str, int],
        cpu_by_proj: Mapping[str, float],
        ram_by_proj: Mapping[str, float],
        gpu_by_proj: Mapping[str, float],
        *,
        cpu: float,
        ram: float,
        gpu: float,
    ) -> str | None:
        """Block reason when starting this task would exceed any ancestor set's total, else None."""
        for path in self.capped_ancestors(project):
            cap = self.caps[path]
            use = self.usage(path, running_by_proj, cpu_by_proj, ram_by_proj, gpu_by_proj)
            if cap["max_tasks"] > 0 and use["running"] + 1 > cap["max_tasks"]:
                return f"set {path} max_tasks={cap['max_tasks']:.0f} reached ({use['running']:.0f} running in the set)"
            if cap["max_cpu_cores"] > 0 and use["cpu"] + cpu > cap["max_cpu_cores"]:
                return f"set {path} max_cpu_cores={cap['max_cpu_cores']:g} reached ({use['cpu']:g} in use)"
            if cap["max_ram_gib"] > 0 and use["ram"] + ram > cap["max_ram_gib"]:
                return f"set {path} max_ram_gib={cap['max_ram_gib']:g} reached ({use['ram']:.1f} in use)"
            if cap["max_gpu_vram_gib"] > 0 and gpu > 0 and use["gpu"] + gpu > cap["max_gpu_vram_gib"]:
                return f"set {path} max_gpu_vram_gib={cap['max_gpu_vram_gib']:g} reached ({use['gpu']:.1f} in use)"
            if cap["max_gpu_hours_day"] > 0 and gpu > 0 and self.gpu_hours(path) >= cap["max_gpu_hours_day"]:
                return (
                    f"set {path} max_gpu_hours_day={cap['max_gpu_hours_day']:g} reached "
                    f"({self.gpu_hours(path):.2f} h used today, UTC)"
                )
        return None

    def over_cap(
        self,
        running_by_proj: Mapping[str, int],
    ) -> list[tuple[str, int]]:
        """``[(set_path, excess_running)]`` for sets running more tasks than their max_tasks."""
        out = []
        for path, cap in self.caps.items():
            mt = int(cap["max_tasks"])
            if mt <= 0:
                continue
            n = sum(running_by_proj.get(p, 0) for p in self.members(path))
            if n > mt:
                out.append((path, n - mt))
        return out
