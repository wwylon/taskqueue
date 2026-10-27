// Per-project colours: the server assigns each project the colour farthest from those in use
// (taskqueue/project_colors.py) and ships them in /api/status as `project_colors`.
import { S } from '../core/state.js';

const _FALLBACK = '#94a3b8';

/** `#rrggbb` for a project (neutral grey until status has loaded). */
export function projColor(name) {
  const m = (S.status && S.status.project_colors) || {};
  return m[name] || _FALLBACK;
}

/** Inline-style fragment that sets the `--pc` custom property (consumed by `.proj-tag` etc.). */
export function projStyle(name) {
  return `--pc:${projColor(name)}`;
}
