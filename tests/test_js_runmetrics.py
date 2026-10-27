"""Behavior tests for components/runmetrics.js (ETA, usage chips, gauge model and in-place dashboard patching)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const r = {};
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_eta_and_cpu_format():
    out = run_js(_js("""
r.eta = [g._etaRemainingSec({ elapsed_sec: 100 }, null, 50), g._etaRemainingSec({ elapsed_sec: 100, prior_elapsed_sec: 100 }, 100, 25),
         g._etaRemainingSec({}, 0, 50), g._etaRemainingSec({ elapsed_sec: 5 }, 5, 0), g._etaRemainingSec({ elapsed_sec: 5 }, 5, null)];
S.cpuTotalCores = 8; r.cores = [g.fmtCpu(250), g.fmtCpu(12.5)];
S.cpuTotalCores = 0; r.pct = g.fmtCpu(36.6);
"""))
    assert out["eta"] == [100, 600, None, None, None]
    assert out["cores"] == ["20.0 cores", "1.0 cores"] and out["pct"] == "37%"


def test_identity_and_structure_keys_ignore_live_fields():
    out = run_js(_js("""
const t = { project: 'p', task_id: 't', run_id: 1, status: 'running', idle_sec: 1, progress_pct: 5, current_cpu_pct: 10 };
const a = g._runningIdentity([t], { frozen_threshold_sec: 100 });
r.sameOnLive = a === g._runningIdentity([{ ...t, current_cpu_pct: 99, progress_pct: 50, idle_sec: 2 }], { frozen_threshold_sec: 100 });
r.frozen = a !== g._runningIdentity([{ ...t, idle_sec: 500 }], { frozen_threshold_sec: 100 });
r.pctKnown = a !== g._runningIdentity([{ ...t, progress_pct: null }], { frozen_threshold_sec: 100 });
r.empty = g._runningIdentity(null, null);
const s = { system: { cpu_total_cores: 4, ram_total_gib: 16, ram_used_gib: 1 } };
const k = g._gaugeStructureKey(s, { cpu_limit_pct: 50 });
r.keySame = k === g._gaugeStructureKey({ system: { cpu_total_cores: 4, ram_total_gib: 16, ram_used_gib: 9 } }, { cpu_limit_pct: 50 });
r.keyDiff = k !== g._gaugeStructureKey(s, { cpu_limit_pct: 60 });
r.keyNull = typeof g._gaugeStructureKey(null, null);
"""))
    assert out["sameOnLive"] and out["frozen"] and out["pctKnown"]
    assert out["empty"] == "[]" and out["keySame"] and out["keyDiff"] and out["keyNull"] == "string"


def test_gpu_badge_log_chips_and_usage_html():
    out = run_js(_js("""
r.gpu = [g._gpuBadgeHtml(null), g._gpuBadgeHtml({}), g._gpuBadgeHtml({ requires_gpu: true }), g._gpuBadgeHtml({ requires_gpu: true, resources: { gpu_vram_gib: 8 } })];
r.noChips = g._logChipsHtml({ log: {} }, null);
const t = { run_id: 7, project: 'p', task_id: 't', log: { warnings: 2, errors: 1, other: 3 } };
const plain = g._logChipsHtml(t, null);
r.plain = [plain.includes('2 warn'), plain.includes('1 err'), plain.includes('3 issue'), plain.includes('openLogIssue')];
const click = g._logChipsHtml(t, { panel: 'q' });
r.click = [click.includes('openLogIssue'), click.includes('log-issue-chip'), click.includes('q-log-row-7'), click.includes('data-click-stop')];
S.cpuTotalCores = 4; S.status = { running: [{ run_id: 7 }] };
const full = g._usageHtml({ run_id: 7, current_cpu_pct: 90, peak_cpu_pct: 95, current_ram_gib: 2, peak_ram_gib: 3, requires_gpu: true, current_vram_gib: 1.5,
  peak_vram_gib: 2, current_gpu_util_pct: 40, avg_gpu_util_pct: 30 });
r.full = ['CPU ', '3.6 cores', 'var(--warn)', '2.0 GiB', '/3.0 GiB pk', 'VRAM', '1.5 GiB', '40%', '30% avg'].map(x => full.includes(x));
const peaks = g._usageHtml({ run_id: 1, peak_cpu_pct: 50, peak_ram_gib: 1, peak_vram_gib: 2, avg_gpu_util_pct: 10 });
r.peaks = ['CPU pk', 'RAM pk', 'VRAM pk', 'GPU avg'].map(x => peaks.includes(x));
r.vram0 = g._usageHtml({ run_id: 1, requires_gpu: true }).includes('0.0 GiB');
r.pending = g._usageHtml({ run_id: 7 }).includes('status.usagePending');
r.idle = g._usageHtml({ run_id: 99 }).includes('—');
"""))
    assert out["gpu"][:2] == ["", ""] and "GPU</span>" in out["gpu"][2] and "GPU 8G" in out["gpu"][3]
    assert out["noChips"] == ""
    assert out["plain"] == [True, True, True, False]
    assert out["click"] == [True, True, True, True]
    assert out["full"] == [True] * 9 and out["peaks"] == [True] * 4
    assert out["vram0"] and out["pending"] and out["idle"]


def test_gauge_model_and_patch_dashboard_live():
    out = run_js(_js("""
S.cfg = { cpu_limit_pct: 50, ram_limit_pct: 50, gpu_limit_pct: 100 };
S.status = { queued_count: 3, daemon_rss_mb: 100, daemon_cpu_pct: 2,
  system: { cpu_total_cores: 8, ram_total_gib: 32, ram_used_gib: 28, gpu_vram_total_gib: 24, gpu_name: 'RTX' },
  running: [{ run_id: 5, resources: { cpu_cores: 2 }, current_vram_gib: 3, elapsed_sec: 61, progress_pct: 50, last_log_line: 'hello', idle_sec: 9 },
            { run_id: 6, resources: {}, elapsed_sec: 5 }] };
const m = g._computeGaugeModel();
r.model = [m.cpuLimit, m.cpuUsed, m.gpuUsed, m.ramPct, m.ramColor];
S.status.system.ram_used_gib = 1; r.green = g._computeGaugeModel().ramColor;
S.status.system.ram_used_gib = 28;
const els = {};
const mk = (sel, id, extra) => { const e = doc.add(id, extra); els[sel] = e; return e; };
const real = g.document.querySelector;
g.document.querySelector = sel => els[sel] || (sel.startsWith('#gauges') || sel.startsWith('[data-') ? null : real.call(g.document, sel));
const sels = ['#gauges [data-gauge="cpu"]', '#gauges [data-gauge="cpu"] [data-g="val"]', '#gauges [data-gauge="ram"] [data-g="val"]',
  '#gauges [data-gauge="ram"] [data-g="fill"]', '#gauges [data-gauge="queued"] [data-g="val"]', '#gauges [data-gauge="daemon"] [data-g="val"]',
  '[data-elapsed-run-id="5"]', '[data-eta-run-id="5"]', '[data-pbar-run-id="5"]', '[data-log-run-id="5"]', '[data-log-run-id="6"]', '[data-last-ts-run-id="5"]'];
sels.forEach((s, i) => mk(s, 'live-el-' + i));
let sparks = 0; g.tqRecordSparks = () => { sparks++; };
g._patchDashboardLive();
const T = s => els[s].textContent;
r.text = [T(sels[1]), T(sels[2]), els[sels[3]].style.width, T(sels[4]), T(sels[5]), els[sels[2]].style.color];
r.run = [T(sels[6]), T(sels[7]), els[sels[8]].style.width, T(sels[9]), T(sels[10]), T(sels[11]), sparks];
S.status = { running: [] }; g._updateRunningTimes(); r.noRunning = sparks;
"""))
    assert out["model"] == [4, 3, 3, 87.5, "var(--fail)"] and out["green"] == "var(--ok)"
    assert out["text"] == ["3 / 4", "28.0 / 16 GiB", "87.5%", "3", "100 MB", "var(--fail)"]
    assert out["run"][0] == "1m01s"
    assert out["run"][1].startswith("50%") and out["run"][2] == "50%"
    assert out["run"][3] == "hello" and out["run"][4] == "log.noOutput" and out["run"][5] == " · last output 9s ago"
    assert out["run"][6] == 1 and out["noRunning"] == 2
