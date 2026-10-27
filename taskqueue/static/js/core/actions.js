// Delegated event actions: replaces inline onclick/onchange/... attributes.
//
//   <button data-click='[["fn", "arg", 1, {"$":"this"}]]'>      calls window.fn("arg", 1, <element>)
//
// Attribute value = JSON list of calls `[name, ...args]` run in order. Arg markers {"$":"this"} (the
// element carrying the attribute) and {"$":"event"} are resolved at dispatch. Optional companions:
//   data-<ev>-stop      stopPropagation after running (ancestor actions / document listeners are skipped)
//   data-<ev>-self      only run when event.target is the element itself (backdrop clicks)
//   data-<ev>-prevent   preventDefault
//   data-keydown-key    only run for this event.key
// A call returning false calls preventDefault (the old `return fn()` idiom).
// Arguments are data, never code, so user-supplied names cannot break out of a JS string.
import { escHtml } from '../util/format.js';

export const EVENTS = ['click', 'change', 'input', 'keydown', 'keyup', 'submit', 'contextmenu', 'mouseenter', 'mouseleave'];
// mouseenter/mouseleave do not bubble: listen in the capture phase and act only on the element itself.
const DIRECT = new Set(['mouseenter', 'mouseleave']);
// Registry of delegated actions. Entries here win over window.<name>, so a view module can register
// its handlers without exposing a global; unregistered names still fall back to window (app.js globals).
const ACTIONS = new Map();
export function registerActions(map) { for (const k of Object.keys(map)) ACTIONS.set(k, map[k]); }
export function hasAction(name) { return ACTIONS.has(name); }

export const THIS = { $: 'this' };
export const EVENT = { $: 'event' };

/** Attribute-safe JSON for data-<ev>="…" in template literals: act([['fn', a, b]]). */
export function act(calls) { return escHtml(JSON.stringify(calls)); }

const _cache = new Map();
function _parse(raw) {
  let v = _cache.get(raw);
  if (v === undefined) {
    try { v = JSON.parse(raw); } catch (e) { v = null; }
    if (_cache.size > 500) _cache.clear();
    _cache.set(raw, v);
  }
  return v;
}

function _resolve(a, el, ev) {
  if (a && typeof a === 'object' && !Array.isArray(a) && typeof a.$ === 'string') {
    return a.$ === 'this' ? el : a.$ === 'event' ? ev : undefined;
  }
  return a;
}

function _runOne(el, ev, type) {
  if (el.hasAttribute(`data-${type}-self`) && ev.target !== el) return false;
  const key = el.getAttribute(`data-${type}-key`);
  if (key && ev.key !== key) return false;
  const calls = _parse(el.getAttribute(`data-${type}`));
  if (!Array.isArray(calls)) return false;
  for (const call of calls) {
    const fn = typeof call[0] === 'string' ? (ACTIONS.get(call[0]) || window[call[0]]) : null;
    if (typeof fn !== 'function') { console.error('[tq] unknown action', call[0]); continue; }
    let r;
    try { r = fn.apply(window, call.slice(1).map(a => _resolve(a, el, ev))); }
    catch (e) { console.error('[tq] action failed', call[0], e); }
    if (r === false) ev.preventDefault();
    if (r && typeof r.catch === 'function') r.catch(e => console.error('[tq] action failed', call[0], e));
  }
  if (el.hasAttribute(`data-${type}-prevent`)) ev.preventDefault();
  return el.hasAttribute(`data-${type}-stop`);
}

export function dispatch(ev) {
  const type = ev.type;
  if (DIRECT.has(type)) {
    const t = ev.target;
    if (t && t.hasAttribute && t.hasAttribute(`data-${type}`)) _runOne(t, ev, type);
    return;
  }
  let el = ev.target && ev.target.closest ? ev.target.closest(`[data-${type}]`) : null;
  while (el) {
    if (_runOne(el, ev, type)) { ev.stopImmediatePropagation(); return; }
    el = el.parentElement ? el.parentElement.closest(`[data-${type}]`) : null;
  }
}

export function installActions(doc) {
  for (const t of EVENTS) doc.addEventListener(t, dispatch, DIRECT.has(t));
}
