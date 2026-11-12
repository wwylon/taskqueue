"""UI text as users see it: the real English catalog (setup({catalog: true})).

Most JS tests run with raw i18n keys; these catch concatenated / interpolated strings
(e.g. "V" + translated "RAM pk") that only break once real text is rendered.
"""

from __future__ import annotations

import json

from tests.test_js_app import run_js


def test_english_catalog_renders_real_text():
    gpu = {
        "processes": {
            "list": [{"pid": 1, "name": "a.exe", "tq": True, "kinds": ["compute"], "vram_gib": 1}],
            "other_gib": 0,
        }
    }
    out = run_js(
        rf"""
const {{ g, S }} = await setup({{ app: true, catalog: true }});
await g.flush();
S.cpuTotalCores = 4; S.status = {{ running: [{{ run_id: 7 }}] }};
const usage = g._usageHtml({{ run_id: 1, peak_cpu_pct: 50, peak_ram_gib: 1, peak_vram_gib: 2, avg_gpu_util_pct: 10 }});
const gpu = g.gpuProcessesHtml({json.dumps(gpu)});
console.log('RESULT:' + JSON.stringify({{
  vram: usage.includes('VRAM pk'), ram: usage.includes('RAM pk'), noKeys: !/runmetric\./.test(usage),
  gpuTitle: gpu.includes('GPU memory by program'), tqBadge: gpu.includes('tq task'),
  t: g.i18n.t('setfilter.bulkSet', {{ set: 'bots', n: 3 }}),
}}));
"""
    )
    assert out == {"vram": True, "ram": True, "noKeys": True, "gpuTitle": True, "tqBadge": True, "t": "set bots (3):"}
