// Status colours shared by the notification panel and the dependency graph (one source of truth).
export const STATUS_HEX = {
  running: '#60a5fa', started: '#60a5fa', queued: '#f59e0b', waiting: '#f59e0b',
  completed: '#34d399', failed: '#e74c3c', interrupted: '#f39c12', suspended: '#c084fc',
  stopped: '#fb7185', cancelled: '#94a3b8', disabled: '#4b5563', unknown: '#a855f7', missing: '#e74c3c',
};

/** `#rrggbb` -> `rgba(r,g,b,alpha)` (graph node fills are the stroke colour at low alpha). */
export function hexToRgba(hex, alpha) {
  const n = parseInt(String(hex).slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}
