"""Behavior tests for components/cfgmodal.js (config modal, per-project limits, JSON request helpers)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: ROUTES });
await g.flush();
const r = {};
"""


def _js(routes: str, body: str) -> str:
    return EV + PRE.replace("ROUTES", routes) + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_send_json_returns_data_and_raises_on_http_or_ok_false():
    out = run_js(_js("{}", """
const seen = [];
let resp = { ok: true, status: 200, json: async () => ({ ok: true, n: 1 }) };
g.fetch = async (url, o) => { seen.push([url, o.method, o.headers['Content-Type'], o.body]); return resp; };
r.ok = await g._postJson('/x', { a: 1 });
r.seen = seen[0];
await g._sendJson('DELETE', '/y');
r.noBody = seen[1][3] === undefined && seen[1][1] === 'DELETE';
const fail = async () => { try { await g._sendJson('POST', '/z', {}); return 'none'; } catch (e) { return e.message; } };
resp = { ok: false, status: 500, json: async () => ({ error: 'boom' }) };
r.httpErr = await fail();
resp = { ok: false, status: 502, json: async () => { throw new Error('bad'); } };
r.httpNoBody = await fail();
resp = { ok: true, status: 200, json: async () => ({ ok: false }) };
r.okFalse = await fail();
resp = { ok: true, status: 200, json: async () => ({ ok: false, error: 'denied' }) };
r.okFalseMsg = await fail();
"""))
    assert out["ok"] == {"ok": True, "n": 1}
    assert out["seen"] == ["/x", "POST", "application/json", '{"a":1}']
    assert out["noBody"] is True
    assert out["httpErr"] == "boom" and out["httpNoBody"] == "502 /z"
    assert out["okFalse"] == "request failed" and out["okFalseMsg"] == "denied"


def test_open_cfg_fills_fields_defaults_and_builds_project_limit_table():
    out = run_js(_js("{ '/api/project-limits': { a: { max_tasks: 3, max_ram_gib: 1.5 } } }", """
for (const id of ['cfg-cpu', 'cfg-gpu', 'cfg-ram', 'cfg-stop', 'cfg-frozen', 'cfg-max-tasks', 'cfg-limit-policy', 'cfg-max-cpu', 'cfg-max-ram', 'cfg-vram-cap', 'cfg-project-limits', 'cfg-modal']) if (!doc.getElementById(id)) doc.add(id);
S.cfg = { cpu_limit_pct: 70, max_running_tasks: 4 };
S.status = { projects: ['a', 'b'] };
await g.openCfg();
const v = id => doc.getElementById(id).value;
r.vals = ['cfg-cpu', 'cfg-gpu', 'cfg-ram', 'cfg-stop', 'cfg-frozen', 'cfg-max-tasks', 'cfg-limit-policy'].map(v);
const html = doc.getElementById('cfg-project-limits').innerHTML;
r.table = [html.includes('data-proj="a" data-field="max_tasks" value="3"'), html.includes('data-field="max_ram_gib" value="1.5"'), html.includes('data-proj="b" data-field="max_tasks" value="0"'), html.includes('step="0.5"')];
r.open = doc.getElementById('cfg-modal').classList.has('open');
g.closeCfg();
r.closed = doc.getElementById('cfg-modal').classList.has('open');
S.status = { projects: [] };
await g.openCfg();
r.empty = doc.getElementById('cfg-project-limits').innerHTML.includes('config.noProjects');
"""))
    assert out["vals"] == [70, 100, 80, 30, 600, 4, "ignore"]
    assert out["table"] == [True, True, True, True]
    assert out["open"] is True and out["closed"] is False and out["empty"] is True


def test_save_cfg_posts_config_and_project_limits_then_closes_or_alerts():
    out = run_js(_js("{ '/api/config': { ok: true }, '/api/project-limits/': { ok: true } }", """
for (const id of ['cfg-cpu', 'cfg-gpu', 'cfg-ram', 'cfg-stop', 'cfg-frozen', 'cfg-max-tasks', 'cfg-limit-policy', 'cfg-max-cpu', 'cfg-max-ram', 'cfg-vram-cap', 'cfg-modal']) if (!doc.getElementById(id)) doc.add(id);
doc.getElementById('cfg-cpu').value = '55'; doc.getElementById('cfg-limit-policy').value = 'stop';
const cont = doc.getElementById('cfg-project-limits') || doc.add('cfg-project-limits');
const mk = (proj, field, val) => { const i = doc.createElement('input'); i.dataset = { proj, field }; i.value = val; return i; };
const inputs = [mk('a', 'max_tasks', '2'), mk('a', 'min_tasks', '1'), mk('b d', 'max_tasks', '5')];
const realQ = g.document.querySelectorAll;
g.document.querySelectorAll = sel => (sel.startsWith('#cfg-project-limits') ? inputs : realQ.call(g.document, sel));
const alerts = []; g.openAlert = (m, t) => alerts.push([m, t]);
let refreshed = 0; g.refresh = () => { refreshed++; };
doc.getElementById('cfg-modal').classList.add('open');
g.fetches.length = 0;
await g.saveCfg();
const posts = g.fetches.filter(f => f.opts && f.opts.method === 'POST');
r.urls = posts.map(f => f.url).sort();
r.cfg = JSON.parse(posts.find(f => f.url === '/api/config').opts.body);
r.limits = JSON.parse(posts.find(f => f.url === '/api/project-limits/a').opts.body);
r.after = [doc.getElementById('cfg-modal').classList.has('open'), refreshed, alerts.length];
g.fetch = async () => ({ ok: false, status: 400, json: async () => ({ error: 'bad cfg' }) });
doc.getElementById('cfg-modal').classList.add('open');
await g.saveCfg();
r.err = [alerts[0], doc.getElementById('cfg-modal').classList.has('open'), refreshed];
"""))
    assert out["urls"] == ["/api/config", "/api/project-limits/a", "/api/project-limits/b%20d"]
    assert out["cfg"]["cpu_limit_pct"] == 55 and out["cfg"]["limit_decrease_policy"] == "stop"
    assert out["limits"] == {"max_tasks": 2, "min_tasks": 1}
    assert out["after"] == [False, 1, 0]
    assert out["err"] == [["bad cfg", "config.title"], True, 1]
