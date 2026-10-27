"""Behavior tests for views/resources.js (Resources tab) under the Node DOM stub."""
from __future__ import annotations

from tests.test_js_app import run_js

CAP = {"cpu_cores_total": 16, "cpu_cores_limit": 8, "ram_gib_limit": 32, "gpu_vram_gib_limit": 0}
HW = {
    "os": "Windows 11", "arch": "AMD64", "cpu_model": "Test CPU", "cpu_physical_cores": 8, "cpu_logical_cores": 16,
    "cpu_util_pct": 95.0, "cpu_temp_c": 90, "cpu_per_core_pct": [10, 95],
    "ram_total_gib": 64.0, "ram_used_gib": 16.0, "ram_available_gib": 48.0, "ram_pct": 25.0,
    "gpu": [{"name": "RTX <b>9000</b>", "load_pct": 50, "mem_total_gib": 24, "mem_used_gib": 12}],
}
STATUS = {
    "running": [
        {"current_cpu_pct": 200, "current_ram_gib": 8, "current_vram_gib": 0, "resources": {"cpu_cores": 2}},
        {"current_cpu_pct": 400, "current_ram_gib": 8, "current_vram_gib": 0, "resources": {"cpu_cores": 4}},
    ],
    "system": {"cpu_total_cores": 16, "ram_used_gib": 20, "ram_total_gib": 64},
}


def test_load_resources_renders_cluster_usage_limits_and_hardware():
    out = run_js(
        f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/capacity': {CAP!r}, '/api/hardware': {HW!r} }} }});
await g.flush();
S.status = {STATUS!r}; S.cfg = {{ cpu_limit_pct: 40, ram_limit_pct: 70, max_running_tasks: 3 }};
await g.loadResources(); await g.flush();
const text = id => doc.getElementById(id).innerHTML;
const r = {{ cores: S.cpuTotalCores }};
r.cluster = text('cluster-usage-bars');
r.bars = text('res-bars');
r.hw = text('hw-content');
r.ts = doc.getElementById('hw-refresh-ts').textContent;
r.inputs = ['res-cpu-pct', 'res-ram-pct', 'res-max-tasks'].map(id => {{ const m = r.bars.split('id="' + id + '" value="')[1]; return m ? m.split('"')[0] : null; }});
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["cores"] == 16
    # 600% of 8 cores (800%) -> 75% bar; 16/32 GiB RAM; no VRAM row without a limit or usage
    assert "600%" in out["cluster"] and "width:75.0%" in out["cluster"] and "16.0 GiB / 32.0 GiB" in out["cluster"]
    assert "VRAM" not in out["cluster"]
    assert "6 / 8" in out["bars"] and "2 / 3" in out["bars"]          # cpu cores used/limit, running/max tasks
    assert out["inputs"] == ["40", "70", "3"]
    assert "Windows 11 · AMD64" in out["hw"] and "8P / 16L" in out["hw"] and "temp-hot" in out["hw"]
    assert "RTX &lt;b&gt;9000&lt;/b&gt;" in out["hw"] and "<b>9000</b>" not in out["hw"]
    assert out["ts"].startswith("updated ")


def test_resources_empty_states_and_panel_toggle():
    out = run_js(
        """
const { g, doc, S } = await setup({ app: true });
await g.flush();
S.status = null;
g.renderResourceBars(null, {});
const r = { noStatus: doc.getElementById('res-bars').innerHTML.includes('resources.noStatus') };
g.renderHardware(null);
r.noHw = doc.getElementById('hw-content').innerHTML.includes('resources.noHardwareData');
g.renderHardware({ gpu_error: 'driver <x>' });
const hw = doc.getElementById('hw-content').innerHTML;
r.gpuErr = hw.includes('hardware.gpuDriverError');
g.renderHardware({});
r.noGpu = doc.getElementById('hw-content').innerHTML.includes('hardware.noGpu');
const body = doc.getElementById('res-panel-body'), arrow = doc.getElementById('res-toggle-arrow');
const was = body.classList.has('open');
g.toggleResPanel(); r.first = [body.classList.has('open'), arrow.textContent];
g.toggleResPanel(); r.second = [body.classList.has('open'), arrow.textContent];
r.was = was;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["noStatus"] and out["noHw"] and out["gpuErr"] and out["noGpu"]
    assert out["first"] == [not out["was"], "▼" if not out["was"] else "▶"]
    assert out["second"] == [out["was"], "▼" if out["was"] else "▶"]


def test_save_res_limits_posts_config_then_refreshes_or_alerts():
    out = run_js(
        """
const posts = [];
const { g, doc } = await setup({ app: true, routes: { '/api/config': (url, opts) => { posts.push(JSON.parse(opts.body)); return posts.length === 1 ? { ok: true } : { __status: 400, body: { error: 'bad limit' } }; } } });
await g.flush();
g.evalApp("S.status = { running: [], system: {} }");
g.renderResourceBars(null, {});
const r = { alerts: [], order: [] };
g.openAlert = m => r.alerts.push(m);
g.refresh = async () => { r.order.push('refresh'); };
doc.getElementById('res-cpu-pct').value = '25';
doc.getElementById('res-ram-pct').value = '60';
doc.getElementById('res-max-tasks').value = '4';
await g.saveResLimits(); await g.flush();
r.first = posts[0]; r.orderOk = r.order.slice();
r.reloaded = g.fetches.some(f => f.url.includes('/api/capacity'));
r.order.length = 0; g.fetches.length = 0;
await g.saveResLimits(); await g.flush();      // server rejects: alert, no refresh, no reload
r.afterFail = r.order.slice();
r.reloadedAfterFail = g.fetches.some(f => f.url.includes('/api/capacity'));
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["first"] == {"cpu_limit_pct": 25, "ram_limit_pct": 60, "max_running_tasks": 4}
    assert out["orderOk"] == ["refresh"] and out["reloaded"] is True
    assert out["afterFail"] == [] and out["reloadedAfterFail"] is False and out["alerts"] == ["bad limit"]
