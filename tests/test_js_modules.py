"""Behavior tests for the ES modules under static/js/ (run through Node; skipped when Node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "taskqueue" / "static" / "js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _run(code: str):
    r = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _fmt_import(names: str) -> str:
    return f"import {{ {names} }} from {json.dumps((JS / 'util' / 'format.js').as_uri())};"


def test_esc_and_jsq_are_attribute_safe():
    out = _run(_fmt_import("escHtml, jsq, taskKey") + """
    console.log(JSON.stringify({ e: escHtml('<a href="x">&'), j: jsq("a'b\\"c"), k: taskKey('p', 't') }));""")
    assert out["e"] == "&lt;a href=&quot;x&quot;&gt;&amp;"
    assert out["k"] == "p/t"
    # jsq output sits inside a double-quoted attribute: no raw quote may survive, and it decodes to the input
    assert '"' not in out["j"].replace("&quot;", "")
    assert out["j"].startswith("&quot;") and out["j"].endswith("&quot;")


def test_dur_and_fmt():
    out = _run(_fmt_import("dur, fmt") + """
    console.log(JSON.stringify([dur(null), dur(-1), dur(5), dur(65), dur(3725), fmt(''), fmt('not a date')]));""")
    assert out == ["—", "—", "5s", "1m05s", "1h02m", "—", "not a date"]


def test_append_log_keeps_bounded_tail():
    out = _run(_fmt_import("appendLog, LOG_CAP_CHARS") + """
    const st = { content: '' };
    for (let i = 0; i < 20000; i++) appendLog(st, 'line-' + i + ' ' + 'x'.repeat(40));
    console.log(JSON.stringify({ len: st.content.length, cap: LOG_CAP_CHARS,
      first: st.content.split('\\n')[0].startsWith('line-'), last: st.content.trimEnd().split('\\n').pop() }));""")
    assert out["len"] <= out["cap"]
    assert out["first"] and out["last"].startswith("line-19999 ")


def test_fetch_json_returns_null_on_failure_and_adds_auth():
    api = (JS / "core" / "api.js").as_uri()
    out = _run(f"""
    const store = {{ tq_token: 'tok' }};
    globalThis.localStorage = {{ getItem: k => store[k] ?? null }};
    const seen = [];
    globalThis.window = globalThis;
    globalThis.fetch = async (url, init) => {{
      seen.push([url, new Headers(init.headers).get('Authorization')]);
      if (url === '/bad') return {{ ok: false, status: 500 }};
      if (url === '/nm') return {{ status: 304, ok: false }};
      return {{ ok: true, status: 200, headers: new Headers({{ ETag: '"e2"' }}), json: async () => ({{ a: 1 }}) }};
    }};
    const m = await import({json.dumps(api)});
    const r = {{ bad: await m.fetchJson('/bad', 1000), good: await m.fetchJson('/ok', 1000),
      nm: await m.fetchJsonEtag('/nm', 1000, 'e1'), et: await m.fetchJsonEtag('/ok', 1000, 'e1'), seen }};
    console.log(JSON.stringify(r));""")
    assert out["bad"] is None and out["good"] == {"a": 1}
    assert out["nm"]["notModified"] is True and out["nm"]["etag"] == "e1"
    assert out["et"]["etag"] == "e2" and out["et"]["data"] == {"a": 1}
    assert all(auth == "Bearer tok" for _, auth in out["seen"])


SHIMS = ("escHtml", "jsq", "taskKey", "qrowId", "domIdPart", "fmt", "dur", "_appendLog", "_LOG_CAP_CHARS",
         "tqEventSource", "_fetchJson", "_fetchJsonEtag", "tqLogLineHtml", "trackLogSse", "LOG_SSE_MAX",
         "_applyLogSearch", "searchLog", "logSearchNav", "openLogStream", "logTailText", "tqAct", "TQ_THIS", "TQ_EVENT",
         "loadAudit", "tqRollbackConfig", "tqExportAudit", "loadApiTokens", "tqCreateToken", "tqRevokeToken",
         "loadSchedules", "tqSelectSchedule", "tqNewSchedule", "tqSchedRun", "tqSchedPause",
         "loadAlerts", "tqPatchSystemAlert", "tqRequestNotifPermission",
         "loadUsage", "setUsageRange", "setUsageType", "tqToggleUsageSeries",
         "toggleHistoryLog", "toggleHistoryCardLog", "tailHistoryCardLog", "tailHistoryLog", "onHistorySearchInput",
         "clearHistorySearch", "setHistoryFilter", "sortHistory", "loadMoreHistory", "renderHistory",
         "toggleHistoryPageHist", "toggleHistoryCardHist",
         "toggleQueueLog", "tailQueueLog", "onQueueSearchInput", "clearQueueSearch", "setQueueFilter", "setQueueProject",
         "sortQueue", "renderQueue", "goQueuePage", "gotoQueueDep", "gotoQueueProject", "_flashQueueRow",
         "toggleDashLog", "refreshDashLog", "tailDashLog", "toggleDashHist", "renderDashboard", "renderWorkers",
         "loadDeps", "renderDag", "showDagTip", "hideDagTip", "dagNodeClick", "hideDagCtxMenu", "showDagCtxMenu", "dagCtxStop",
         "dagCtxRetry", "dagCtxForce", "dagCtxMarkProcessed", "dagCtxEnable", "tqDagZoomBy", "tqDagScaleFrom", "_dagResetView",
         "loadGroups", "groupStop", "groupRetry", "_populateGroupChips", "_updateGroupChips", "_toggleGroupBar", "setGroupFilter",
         "viewGroupInQueue",
         "loadResources", "loadHardware", "renderClusterUsage", "renderResourceBars", "renderHardware", "saveResLimits",
         "toggleResPanel",
         "_closeGenModal", "openModal", "openAlert", "openConfirm", "openYamlModal", "closeYamlModal", "yamlModalToggleEdit", "yamlModalSave", "getResourceModalRunId", "_resourceModalBodyHtml", "showResourceModal", "compareRun", "closeResourceModal", "yamlModalCopy", "showToast", "showAnchorToast",
         "toggleNotifPanel", "_notifMarkRead", "tqAlertDetailHtml", "tqToggleAlertDetail", "_openAlertsTab", "_fetchNotifications", "_renderNotifPanel",
         "tqSetNotifTypeFilter", "tqSetNotifProjectFilter", "tqRenderFilterSummary")


def test_views_avoid_window_shim_lookups():
    """Views must not call through window.<fn> (use imports / registerActions). Geometry APIs allowed."""
    import re

    allow = re.compile(
        r"window\.(?:addEventListener|removeEventListener|innerWidth|innerHeight|"
        r"matchMedia|devicePixelRatio|getComputedStyle|location|scrollTo|"
        r"requestAnimationFrame|cancelAnimationFrame)\b"
    )
    bad = []
    for path in sorted((JS / "views").glob("*.js")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "window." not in line:
                continue
            # Strip comments.
            code = line.split("//", 1)[0]
            if "window." not in code:
                continue
            # typeof window / window !== undefined guards are fine.
            if re.search(r"typeof\s+window\b|window\s*!==|window\s*===", code):
                continue
            for m in re.finditer(r"window\.([A-Za-z_$][\w$]*)", code):
                frag = m.group(0)
                if allow.search(frag):
                    continue
                bad.append(f"{path.name}:{n}:{frag}")
    assert not bad, f"views must not look up shims on window: {bad}"


def test_main_exposes_every_shim_on_window():
    main = (JS / "main.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    await import({json.dumps(main)});
    console.log(JSON.stringify({json.dumps(list(SHIMS))}.map(n => [n, typeof window[n]])));""")
    missing = [n for n, t in out if t == "undefined"]
    assert not missing, missing


def test_app_js_does_not_redefine_module_helpers():
    import re

    app = (JS.parent / "app.js").read_text(encoding="utf-8")
    for name in SHIMS:
        assert not re.search(rf"^(?:async )?function {name}\b|^(?:const|let|var) {name}\b", app, re.M), name


def _logview_import(names: str) -> str:
    return f"import {{ {names} }} from {json.dumps((JS / 'components' / 'logview.js').as_uri())};"


def test_log_line_html_strips_ansi_escapes_and_classes_level():
    out = _run(_logview_import("logLineHtml") + r"""
    console.log(JSON.stringify([logLineHtml('[31m[ERROR] <b>boom</b>[0m'), logLineHtml('plain'), logLineHtml('[WARN] x')]));""")
    assert out[0] == '<div class="tq-log-line log-error">[ERROR] &lt;b&gt;boom&lt;/b&gt;</div>'
    assert out[1] == '<div class="tq-log-line">plain</div>'
    assert "log-warn" in out[2]


def test_track_log_sse_replaces_same_run_and_evicts_oldest_across_maps():
    out = _run(_logview_import("trackLogSse, LOG_SSE_MAX") + r"""
    const closed = [];
    const mk = id => ({ close() { closed.push(id); } });
    const a = {}, b = {};
    trackLogSse([a, b], a, 1, mk('a1'));
    trackLogSse([a, b], a, 1, mk('a1b'));      // same run: previous stream closed
    trackLogSse([a, b], b, 2, mk('b2'));
    trackLogSse([a, b], a, 3, mk('a3'));
    trackLogSse([a, b], b, 4, mk('b4'));        // over cap: oldest (run 1) evicted
    console.log(JSON.stringify({ closed, a: Object.keys(a), b: Object.keys(b), max: LOG_SSE_MAX }));""")
    assert out["max"] == 3
    assert out["closed"] == ["a1", "a1b"]
    assert sorted(out["a"] + out["b"]) == ["2", "3", "4"]


def test_log_search_records_state_and_navigates_highlights():
    lp = (JS / "components" / "logpanel.js").as_uri()
    st = (JS / "core" / "state.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const spans = [0, 1, 2].map(() => ({{ style: {{}}, scrollIntoView() {{}} }}));
    const count = {{ textContent: '' }};
    const pre = {{ querySelectorAll: () => spans }};
    globalThis.document = {{ getElementById: id => id === 'p1' ? pre : id === 'log-search-count-p1' ? count : null }};
    const lp = await import({json.dumps(lp)});
    const {{ S }} = await import({json.dumps(st)});
    lp.searchLog({{ value: '  Boom ' }}, 7, 'missing');       // pre absent: records state, no throw
    S.logSearch.p1 = {{ query: 'x', current: 2 }};
    lp.logSearchNav('p1', 1);                                // wraps 2 -> 0
    const wrapped = S.logSearch.p1.current;
    lp.logSearchNav('p1', -1);                               // wraps 0 -> 2
    console.log(JSON.stringify({{ missing: S.logSearch.missing, wrapped, back: S.logSearch.p1.current,
      count: count.textContent, active: spans[2].style.background }}));""")
    assert out["missing"] == {"query": "boom", "current": 0}
    assert out["wrapped"] == 0 and out["back"] == 2
    assert out["count"] == "3 / 3" and out["active"] == "#ef4444"


def test_log_tail_text_normalises_newlines_and_limits_lines():
    lp = (JS / "components" / "logpanel.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const {{ logTailText }} = await import({json.dumps(lp)});
    const nl = String.fromCharCode(10), cr = String.fromCharCode(13);
    const text = ['a', 'b', 'c', 'd'].join(cr + nl) + nl + nl;
    console.log(JSON.stringify([logTailText(text, 2).split(nl), logTailText('', 5), logTailText('x', 5)]));""")
    assert out == [["c", "d"], "", "x"]


def test_open_log_stream_appends_follows_and_falls_back_to_static_tail():
    lp = (JS / "components" / "logpanel.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const nl = String.fromCharCode(10);
    const pre = {{ textContent: 'stale', scrollTop: 0, scrollHeight: 99, appended: [] }};
    const follow = {{ checked: true }};
    const els = {{ pre, follow }};
    globalThis.document = {{ getElementById: id => els[id] || null,
      createElement: () => ({{ style: {{}}, textContent: '' }}) }};
    pre.appendChild = n => pre.appended.push(n.textContent);
    class ES {{ constructor(url) {{ this.url = url; ES.last = this; }} addEventListener() {{}} close() {{ this.closed = true; }} }}
    ES.CLOSED = 2;
    globalThis.EventSource = ES;
    const fetched = [];
    window._sseAppend = (p, html) => p.appended.push(html);
    window._ansiToHtml = t => '<' + t.trim() + '>';
    window._fetchLog = (id, pid) => fetched.push([id, pid]);
    window.i18n = {{ t: k => k }};
    const {{ openLogStream }} = await import({json.dumps(lp)});
    const map = {{}}, expanded = {{ 'q:7': {{ content: '', isRunning: true }} }};
    const sse = openLogStream({{ runId: 7, maps: [map], map, preId: 'pre', followId: 'follow', expanded, stateKey: 'q:7' }});
    const cleared = pre.textContent === '';
    sse.onmessage({{ data: 'hello' }});
    const afterMsg = {{ html: pre.appended.slice(), content: expanded['q:7'].content, top: pre.scrollTop, reg: Object.keys(map) }};
    pre.textContent = '';                       // nothing rendered yet
    sse.readyState = ES.CLOSED; sse.onerror();
    console.log(JSON.stringify({{ cleared, afterMsg, fetched, closed: sse._closed, reg: Object.keys(map) }}));""")
    assert out["cleared"]
    assert out["afterMsg"]["html"] == ["<hello>"] and "hello" in out["afterMsg"]["content"]
    assert out["afterMsg"]["top"] == 99 and out["afterMsg"]["reg"] == ["7"]
    assert out["fetched"] == [[7, "pre"]] and out["closed"] and out["reg"] == []


def test_open_log_stream_lets_browser_retry_a_dropped_connection():
    lp = (JS / "components" / "logpanel.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const pre = {{ textContent: 'line', scrollTop: 0, scrollHeight: 1, appendChild() {{}} }};
    globalThis.document = {{ getElementById: id => (id === 'pre' ? pre : null), createElement: () => ({{ style: {{}} }}) }};
    class ES {{ constructor() {{ this.l = {{}}; ES.last = this; this.readyState = 0; }}
      addEventListener(n, f) {{ this.l[n] = f; }} close() {{ this.closed = true; }} }}
    ES.CONNECTING = 0; ES.CLOSED = 2;
    globalThis.EventSource = ES;
    window._fetchLog = () => {{}}; window.i18n = {{ t: k => k }};
    const {{ openLogStream }} = await import({json.dumps(lp)});
    const map = {{}};
    const sse = openLogStream({{ runId: 1, maps: [map], map, preId: 'pre', followId: 'f', expanded: {{}}, stateKey: 'k' }});
    sse.onerror();                                  // transient drop: browser retries
    const afterDrop = !!sse._closed;
    sse._ls[0][1]();                                  // server finished
    sse.onerror();
    console.log(JSON.stringify({{ afterDrop, closed: !!sse._closed }}));""")
    assert out["afterDrop"] is False and out["closed"] is True


def test_qrow_id_is_unambiguous_across_projects():
    fm = (JS / "util" / "format.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const {{ qrowId }} = await import({json.dumps(fm)});
    console.log(JSON.stringify({{ a: qrowId('a-b', 'c'), b: qrowId('a', 'b-c') }}));""")
    assert out["a"] != out["b"] and out["a"].startswith("qrow-")


def test_actions_dispatch_runs_calls_with_markers_flags_and_stops():
    act = (JS / "core" / "actions.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const log = [];
    window.rec = (...a) => {{ log.push(a.map(x => x && x.id ? '#' + x.id : x && x.type ? 'ev:' + x.type : x)); }};
    window.ret = () => false;
    window.boom = () => {{ throw new Error('x'); }};
    const mk = (id, attrs, parent) => {{
      const el = {{ id, attrs, parentElement: parent || null,
        hasAttribute(n) {{ return n in attrs; }}, getAttribute(n) {{ return attrs[n] ?? null; }},
        closest(sel) {{ const n = sel.slice(1, -1); for (let e = this; e; e = e.parentElement) if (n in e.attrs) return e; return null; }} }};
      return el;
    }};
    const m = await import({json.dumps(act)});
    const ev = (type, target, extra = {{}}) => ({{ type, target, defaultPrevented: false, stopped: false,
      preventDefault() {{ this.defaultPrevented = true; }}, stopImmediatePropagation() {{ this.stopped = true; }}, ...extra }});
    const outer = mk('o', {{ 'data-click': JSON.stringify([['rec', 'outer']]) }});
    const inner = mk('i', {{ 'data-click': JSON.stringify([['rec', 'a', 1, {{ $: 'this' }}], ['rec', {{ $: 'event' }}], ['ret'], ['nope'], ['boom']]) }}, outer);
    const e1 = ev('click', inner); m.dispatch(e1);              // inner then ancestor, false -> preventDefault
    const stopEl = mk('s', {{ 'data-click': JSON.stringify([['rec', 's']]), 'data-click-stop': '' }}, outer);
    const e2 = ev('click', stopEl); m.dispatch(e2);             // stop: ancestor skipped
    const selfEl = mk('b', {{ 'data-click': JSON.stringify([['rec', 'back']]), 'data-click-self': '' }});
    const child = mk('c', {{}}, selfEl); child.attrs = {{}};
    m.dispatch(ev('click', selfEl)); m.dispatch(ev('click', {{ ...child, closest: () => selfEl }}));   // only the first runs
    const keyEl = mk('k', {{ 'data-keydown': JSON.stringify([['rec', 'enter']]), 'data-keydown-key': 'Enter' }});
    m.dispatch(ev('keydown', keyEl, {{ key: 'a' }})); m.dispatch(ev('keydown', keyEl, {{ key: 'Enter' }}));
    const enter = mk('h', {{ 'data-mouseenter': JSON.stringify([['rec', 'tip']]) }});
    const kid = mk('hk', {{}}, enter); kid.attrs = {{}};
    m.dispatch(ev('mouseenter', kid)); m.dispatch(ev('mouseenter', enter));   // direct only
    console.log(JSON.stringify({{ log, p1: e1.defaultPrevented, s1: e1.stopped, s2: e2.stopped,
      act: m.act([['f', 'a"b<c', 1]]) }}));""")
    assert out["log"] == [["a", 1, "#i"], ["ev:click"], ["outer"], ["s"], ["back"], ["enter"], ["tip"]]
    assert out["p1"] and not out["s1"] and out["s2"]
    assert '"' not in out["act"] and "<" not in out["act"]


def test_esc_html_escapes_single_quote_and_fmt_escapes_invalid():
    out = _run(_fmt_import("escHtml, fmt") + """
    console.log(JSON.stringify({ e: escHtml("a'b"), f: fmt('<img src=x>') }));""")
    assert out["e"] == "a&#39;b"
    assert out["f"] == "&lt;img src=x&gt;"


def test_latest_drops_stale_responses():
    # api.js installs a fetch wrapper at import time; give Node the globals it touches.
    api = json.dumps((JS / "core" / "api.js").as_uri())
    out = _run("globalThis.window = globalThis; globalThis.localStorage = { getItem() { return null; } };"
               "globalThis.fetch = () => {}; globalThis.Headers = Map;"
               f"const {{ latest }} = await import({api});"
               "const a = latest('k'); const b = latest('k'); const c = latest('other');"
               "console.log(JSON.stringify([a(), b(), c()]));")
    assert out == [False, True, True]


def test_every_state_field_assigned_by_modules_is_declared_in_state_js():
    import re

    static = Path(__file__).resolve().parent.parent / "taskqueue" / "static"
    state = (static / "js" / "core" / "state.js").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*([A-Za-z_]\w*)\s*:", state, re.M))
    assigned = set()
    for f in [*static.glob("js/**/*.js"), static / "app.js"]:
        assigned |= set(re.findall(r"\bS\.([A-Za-z_]\w*)\s*=(?!=)", f.read_text(encoding="utf-8")))
    assert not sorted(assigned - declared), "declare these in core/state.js"


def test_proj_color_reads_status_map_with_grey_fallback():
    state = json.dumps((JS / "core" / "state.js").as_uri())
    pc = json.dumps((JS / "util" / "projcolor.js").as_uri())
    out = _run(f"""import {{ S }} from {state}; import {{ projColor, projStyle }} from {pc};
    const before = projColor('a');
    S.status = {{ project_colors: {{ a: '#112233' }} }};
    console.log(JSON.stringify([before, projColor('a'), projColor('zz'), projStyle('a')]));""")
    assert out == ["#94a3b8", "#112233", "#94a3b8", "--pc:#112233"]


def test_ansi_to_html_colours_keywords_per_line():
    out = _run(_logview_import("ansiToHtml") + r"""
    console.log(JSON.stringify(ansiToHtml('ok line\nERROR bad\nfine')));""")
    assert out == 'ok line\n<span style="color:#e74c3c">ERROR bad</span>\nfine'


def test_i18n_t_does_not_expand_replacement_patterns():
    src = (JS.parent / "i18n.js").read_text(encoding="utf-8")
    code = (
        "globalThis.window = {}; globalThis.document = {querySelectorAll: () => [], documentElement: {}};"
        "globalThis.localStorage = {getItem: () => 'en'};"
        "globalThis.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({k: 'task {name} done'})});"
        + src.replace("load(stored);", "_catalog = {k: 'task {name} done'};")
        + "console.log(JSON.stringify(window.i18n.t('k', {name: \"a$&b$1\"})));"
    )
    assert _run(code) == "task a$&b$1 done"


def test_a11y_annotates_dialogs_chips_and_sort_headers():
    out = _run("""
    import { annotateDialogs, syncStateAttrs } from %s;
    const mk = (cls, attrs = {}) => { const a = {}; const set = new Set(cls);
      return { classList: { contains: c => set.has(c) }, getAttribute: k => a[k] ?? null, setAttribute: (k, v) => { a[k] = v; }, a, querySelector: () => ({ id: 'ttl' }) }; };
    const modal = mk(['modal']), on = mk(['filter-chip', 'active']), off = mk(['filter-chip']), asc = mk(['sortable', 'sort-asc']), none = mk(['sortable']);
    const root = { querySelectorAll: sel => sel.startsWith('.modal-bg') ? [modal] : sel === '.filter-chip' ? [on, off] : [asc, none] };
    annotateDialogs(root); syncStateAttrs(root);
    console.log(JSON.stringify([modal.a, on.a, off.a, asc.a, none.a]));""" % json.dumps((JS / "util" / "a11y.js").as_uri()))
    assert out[0] == {"role": "dialog", "aria-modal": "true", "aria-labelledby": "ttl"}
    assert out[1]["aria-pressed"] == "true" and out[2]["aria-pressed"] == "false"
    assert out[3]["aria-sort"] == "ascending" and out[4]["aria-sort"] == "none"


def test_event_source_never_puts_long_lived_token_in_url():
    api = json.dumps((JS / "core" / "api.js").as_uri())
    out = _run("""
    const urls = [];
    globalThis.window = globalThis;
    globalThis.localStorage = { getItem: () => 'SECRET' };
    globalThis.EventSource = class { constructor(u) { urls.push(u); } close() {} };
    globalThis.fetch = async () => ({ ok: false });
    const { tqEventSource } = await import(%s);
    let errs = 0;
    const h = tqEventSource('/api/stream'); h.onerror = () => { errs++; };
    await new Promise(r => setTimeout(r, 50));
    console.log(JSON.stringify({ urls, errs, rs: h.readyState }));""" % api)
    assert out["urls"] == [] and out["errs"] == 1 and out["rs"] == 2


def test_registered_actions_win_over_window_and_main_registers_exports():
    act = (JS / "core" / "actions.js").as_uri()
    main = (JS / "main.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    globalThis.localStorage = {{ getItem: () => null }};
    const m = await import({json.dumps(act)});
    const log = [];
    window.f = () => log.push('window');
    const el = {{ attrs: {{ 'data-click': '[["f"],["g"]]' }}, parentElement: null,
      hasAttribute(n) {{ return n in this.attrs; }}, getAttribute(n) {{ return this.attrs[n] ?? null; }},
      closest() {{ return this; }} }};
    const ev = {{ type: 'click', target: el, preventDefault() {{}}, stopImmediatePropagation() {{}} }};
    m.registerActions({{ f: () => log.push('registry'), g: () => log.push('g-only-registry') }});
    m.dispatch(ev);
    await import({json.dumps(main)});
    console.log(JSON.stringify({{ log, hasEsc: m.hasAction('openModal'), hasNone: m.hasAction('zzz') }}));""")
    assert out["log"] == ["registry", "g-only-registry"]
    assert out["hasEsc"] and not out["hasNone"]


def test_quiet_hours_window_and_notify_gate():
    prefs = (JS / "components" / "prefs.js").as_uri()
    out = _run(f"""
    globalThis.window = globalThis;
    const m = await import({json.dumps(prefs)});
    const at = (h, mi) => new Date(2026, 0, 1, h, mi);
    console.log(JSON.stringify([
      m.tqInQuietHours('22:00-07:00', at(23, 0)), m.tqInQuietHours('22:00-07:00', at(6, 59)),
      m.tqInQuietHours('22:00-07:00', at(7, 0)), m.tqInQuietHours('22:00-07:00', at(12, 0)),
      m.tqInQuietHours('09:00-17:00', at(9, 0)), m.tqInQuietHours('09:00-17:00', at(17, 0)),
      m.tqInQuietHours('', at(1, 0)), m.tqInQuietHours('junk', at(1, 0)), m.tqInQuietHours('25:00-07:00', at(1, 0)),
      m.tqInQuietHours('08:00-08:00', at(8, 0))]));""")
    assert out == [True, True, False, False, True, False, False, False, False, False]


def test_data_version_gates_queue_refresh():
    out = _run(
        "globalThis.window = globalThis;"
        f"const {{ S }} = await import({json.dumps((JS / 'core' / 'state.js').as_uri())});"
        f"const {{ _dataVersionChanged: ch }} = await import({json.dumps((JS / 'core' / 'refresh.js').as_uri())});"
        "const r = [ch(5), ch(5), ch(6), ch(null), ch(undefined)];"
        "console.log(JSON.stringify([r, S.lastDataVersion]));"
    )
    # first sight and changes refresh; a repeat does not; servers without data_version always refresh
    assert out == [[True, False, True, True, True], None]


def test_queue_structural_key_ignores_live_fields():
    out = _run(
        "globalThis.window = globalThis;"
        f"const {{ _queueStructuralKey: k }} = await import({json.dumps((JS / 'core' / 'refresh.js').as_uri())});"
        "const q = { total: 1 };"
        "const a = [{ project: 'p', task_id: 't', status: 'running', priority: 50, run_id: 9,"
        "  elapsed_sec: 1, progress_pct: 10, idle_sec: 0, enabled: true }];"
        "const b = [{ ...a[0], elapsed_sec: 99, progress_pct: 90, idle_sec: 5 }];"
        "const c = [{ ...a[0], status: 'failed' }];"
        "console.log(JSON.stringify([k(q, a) === k(q, b), k(q, a) === k(q, c)]));"
    )
    assert out == [True, False]


def test_tag_chips_are_capped_but_keep_the_active_tag():
    out = _run(
        "globalThis.window = globalThis;"
        "globalThis.tqAct = (x) => JSON.stringify(x);"
        "const bar = { dataset: { expanded: '1' }, style: {}, innerHTML: '' };"
        "globalThis.document = { getElementById: () => bar };"
        f"const {{ S }} = await import({json.dumps((JS / 'core' / 'state.js').as_uri())});"
        f"const fb = await import({json.dumps((JS / 'components' / 'filterbars.js').as_uri())});"
        "S.allTags = Array.from({ length: 200 }, (_, i) => 't' + i);"
        "S.tagFilter = 't150';"
        "fb._updateTagChips();"
        "const n = (bar.innerHTML.match(/<button/g) || []).length;"
        "console.log(JSON.stringify([n, fb.TAG_CHIP_CAP, bar.innerHTML.includes('t150'), bar.innerHTML.includes('+139 more')]));"
    )
    assert out == [61, 60, True, True]
