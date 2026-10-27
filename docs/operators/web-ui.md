# Web UI

Operator tour for the daemon UI (`http://127.0.0.1:8765`). Live updates come from `GET /api/events` (SSE). `?` in the header opens keyboard shortcuts; **Operator help** inside that overlay opens the in-page help modal, which loads the English topics in `docs/help/`. The HTTP reference is `GET /api/docs`.

The header project filter applies to Queue counts and to Tasks, Timeline, and Schedule. The worker filter does not change the four status counts. Counts come from `GET /api/status?project=` (all tasks in that project, not only the visible page). If an SSE payload omits `status_counts`, the numbers stay as they were.

The **health light** left of the counts is the queue-wide verdict from `tq health` (it ignores the project filter): 🔴 a task failed since the last acknowledge (pulsing), 🟠 acknowledged failures, cap-interrupted tasks or frozen runs, 🟢 nothing needs a human. Icons say what each count is (❌ new failed, ⚠️ acknowledged failed, ⏸️ interrupted, 🧊 frozen); hover lists the tasks, click opens the matching status filter. **Acknowledge** (shown while red) turns it amber until the next failure and is audited as `health_ack`. The tab title gets the same 🔴 / 🟠 prefix and the favicon a coloured dot, red when the daemon stops answering. API: `GET /api/health`, `POST /api/health/ack`; `/api/status` carries the same object as `health`.

Loads you start (switching tabs, the ⟳ / Refresh buttons, the project filter, Timeline hours) show a thin animated bar at the top once they take longer than 300 ms, and a page shows a spinner until its first load finishes. Background refreshes stay silent.

Language (**English / Polski / 中文 / العربية**, each named in its own language) is the select in the sidebar footer. Arabic sets `dir=rtl` and moves the sidebar to the right. Preferences (⊞) are theme **Dark / Auto / Light**, font size, quiet hours, and an **operator** name (same identity as `TQ_OPERATOR` / `X-TQ-Operator`). When an operator is set, the Queue/History default to **Mine** (tasks with matching `started_by`); **View all** (or the **all ops** chip) clears that scope. Project chips in Mine mode also narrow to projects returned by `GET /api/my-projects`. Auto theme follows the OS color scheme.

Below 768px the rail is icon-only. Labels return when the window is wider.

{{topic:page-queue}}

{{topic:page-dashboard}}

{{topic:page-history}}

{{topic:page-workers}}

{{topic:page-resources}}

{{topic:page-usage}}

{{topic:page-groups}}

{{topic:groups}}

{{topic:page-deps}}

{{topic:dependencies}}

{{topic:page-tasks}}

{{topic:page-timeline}}

{{topic:page-schedule}}

{{topic:page-alerts}}

{{topic:page-audit}}

{{topic:page-api}}

Token and CSRF details are in [security](../security.md).

{{topic:block-project}}

{{topic:force-run}}

{{topic:tq-metric}}

{{topic:prod-vs-dev}}

## Offline shell (PWA)

The UI registers a service worker (`/sw.js`, served by the daemon with a cache version derived from the static asset sizes and mtimes) and a web manifest (`/manifest.webmanifest`), so it can be installed as an app and the page shell still loads when the daemon is unreachable. Static assets are fetched network-first, so an upgraded daemon is picked up immediately; the cache is only the offline fallback. `/api/*` and `/docs/*` are never cached, so offline you see the last rendered page plus an **Offline** banner, not stale queue data. Service workers need a secure context: they work on `http://127.0.0.1` / `localhost` and over HTTPS, but not on plain HTTP to another host.

## Contributing to the UI scripts

`taskqueue/static/app.js` is a classic script that is being split into ES modules under `taskqueue/static/js/`. `js/main.js` loads first, imports the modules and exposes the functions app.js and the HTML still call by bare name as `window.*` shims. It also installs the click/keydown delegation. Shared UI state lives in `js/core/state.js` (`S`, exposed as `window.tqState`); put new globals there, not in app.js.

- `core/api.js` (fetch and SSE helpers), `core/state.js`, `core/actions.js` (event delegation), `util/format.js` (escaping and formatting), `components/logview.js` and `components/logpanel.js` (log rendering, search, run-log toggles). Other components: `components/modals.js` (generic/YAML/metrics modals and toasts), `components/notifications.js` (bell and alert panel), `components/prefs.js` (theme, font, language popup) `components/drawer.js` (task drawer), `components/sparks.js` (gauge sparklines) `components/taskactions.js` (stop/retry/force/priority, project freeze/stop-all, worker sync), `components/cfgmodal.js` (config modal and the `_sendJson`/`_postJson` helpers), `components/queuefilters.js` (saved filters, selection, bulk actions, export) `components/shortcuts.js` (keyboard shortcuts) `components/mobpicker.js` (mobile task picker), `components/filterbars.js` (project/worker selects, chips, broken-deps banner), `components/runmetrics.js` (running-task metrics and live dashboard patching), `components/runlogs.js` (per-run log plumbing and history toggles) and `components/logdock.js` (bottom log dock). `util/labels.js` holds badges, wait/error labels and resource tags. `core/loader.js` holds the tab loader, `tqLoop` and `tqBusy`; `core/nav.js` holds tab switching and queue jumps. `core/url.js` holds URL state and project selection; `core/refresh.js` holds the refresh cycle, health/offline indicators and SSE live updates.
- `views/*.js` hold one tab each: queue, dashboard, history, usage, audit, tokens, schedule, alerts, deps, groups, resources, timeline, taskstats and docs (`views/docs.js` also holds the built-in help drawer). Views call the helpers that remain in app.js (`i18n`, `badge`, `refresh`, ...) by global name at call time, so they need no import.
- **No inline `onclick=` / `onchange=`.** Write `data-click="${tqAct([['fnName', arg, TQ_THIS]])}"`: a JSON list of calls whose arguments are data, delegated by `core/actions.js`, which looks up `window[fnName]`. So every function named in a `tqAct` call or in an `index.html` `data-*` attribute needs a shim in `main.js`. Optional flags: `data-click-stop`, `data-click-self`, `data-click-prevent`, `data-keydown-key`. `tests/test_ui_inline_handlers.py` enforces this.
- List every new file under `js/` in the `SHELL` array in `static/sw.js` (`tests/test_pwa_shell.py` checks it) and add its shims to `SHIMS` in `tests/test_js_modules.py`.
- Test modules with Node: `tests/test_js_modules.py` for the core and shared helpers, `tests/test_js_views.py` for views (app.js globals stubbed). Each extracted module also has its own `tests/test_js_<name>.py` (deps, groups, resources, modals, notifications, prefs, drawer, docs, timeline, sparks, url, refresh, taskactions, cfgmodal, queuefilters, shortcuts, mobpicker, taskstats, filterbars, labels, runmetrics, runlogs, logdock, loader, nav) running under the Node DOM stub `tests/js_dom_stub.mjs`.
