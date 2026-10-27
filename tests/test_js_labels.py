"""Behavior tests for util/labels.js (badge, wait/error labels, resource tags, copy button, sort marks)."""
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


def test_badge_plain_waiting_kinds_and_class_sanitising():
    out = run_js(_js("""
r.plain = g.badge('running');
r.weird = g.badge('a b<c>');
r.waitOk = g.badge('queued', 'tq.block.resources', '');
r.waitAttn = g.badge('queued', 'tq.block.frozen', '');
r.waitBad = g.badge('queued', 'tq.block.deps', 'dead dependency x');
r.queuedNoCode = g.badge('queued', 'tq.infra.hung', '');
r.notBlock = g.badge('failed', 'tq.block.resources');
"""))
    assert out["plain"] == '<span class="badge badge-running">running</span>'
    assert out["weird"] == '<span class="badge badge-abc">a b&lt;c&gt;</span>'
    assert out["waitOk"] == '<span class="badge badge-waiting badge-waiting-ok">waiting</span>'
    assert 'badge-waiting-attn' in out["waitAttn"]
    assert 'badge-waiting-bad' in out["waitBad"]
    assert out["queuedNoCode"] == '<span class="badge badge-queued">queued</span>'
    assert 'badge-failed' in out["notBlock"]


def test_wait_kind_and_friendly_error_text():
    out = run_js(_js("""
r.kinds = [
  g._waitKind('tq.block.deps', 'waiting on a/b'), g._waitKind('tq.block.deps', ''), g._waitKind('tq.block.deps', 'x (not found)'),
  g._waitKind('tq.block.deps', 'a [failed]'), g._waitKind('tq.block.mutex'), g._waitKind('tq.block.paused'),
  g._waitKind('tq.block.other'), g._waitKind(null, null),
];
r.text = [
  g.friendlyErrorText('tq.stop.ram', ''), g.friendlyErrorText('tq.stop.ram', 'custom reason'), g.friendlyErrorText('tq.stop.ram', 'tq.raw'),
  g.friendlyErrorText('tq.unknown', 'tq.raw'), g.friendlyErrorText('tq.unknown', ''), g.friendlyErrorText('', ''), g.friendlyErrorText(null, '  spaced  '),
];
"""))
    assert out["kinds"] == ["ok", "attn", "bad", "bad", "ok", "attn", "attn", "attn"]
    assert out["text"] == ["Stopped — host RAM hard limit", "custom reason", "Stopped — host RAM hard limit",
                           "tq.raw", "tq.unknown", "", "spaced"]


def test_wait_reason_html_by_status_and_escaping():
    out = run_js(_js("""
r.running = g.waitReasonHtml({ status: 'running', last_error_code: 'tq.run.cap' });
r.empty = g.waitReasonHtml({ status: 'queued' });
r.suspended = g.waitReasonHtml({ status: 'suspended' });
r.ok = g.waitReasonHtml({ status: 'queued', last_error_code: 'tq.block.resources', block_reason: '' });
r.bad = g.waitReasonHtml({ status: 'queued', last_error_code: 'tq.block.deps', block_reason: 'dead dependency <x>' });
r.noText = g.waitReasonHtml({ status: 'failed', last_error_code: '', block_reason: '' });
"""))
    assert out["running"] == "" and out["empty"] == "" and out["noText"] == ""
    assert 'wait-ok' in out["suspended"] and 'task.suspendedCooperative' in out["suspended"]
    assert 'wait-ok' in out["ok"] and 'Waiting on resources' in out["ok"] and 'title="tq.block.resources"' in out["ok"]
    assert 'wait-bad' in out["bad"] and 'dead dependency &lt;x&gt;' in out["bad"] and 'tq.block.deps — dead' in out["bad"]


def test_res_copy_button_and_sort_header_marks():
    out = run_js(_js("""
r.none = g.res(null);
r.parts = g.res({ cpu_cores: 2, gpu_vram_gib: 8, ram_gib: 16 });
r.peaks = g.res(null, 1.234, 2.5);
r.mixed = g.res({ ram_gib: 4 }, 0, null);
r.btn = g._taskCopyIconBtn('p<1>', 't');
for (const f of ['priority', 'status', 'task_id']) doc.add('q-th-' + f);
doc.getElementById('q-th-status').classList.add('sort-asc');
g._updateSortTh('q-', 'task_id', -1);
const c = f => ['sort-asc', 'sort-desc'].filter(k => doc.getElementById('q-th-' + f).classList.has(k));
r.sort = [c('priority'), c('status'), c('task_id')];
g._updateSortTh('q-', 'priority', 1);
r.asc = c('priority');
"""))
    assert out["none"] == "—"
    assert out["parts"] == '<span class="res-tag">2cpu 8G gpu 16G ram</span>'
    assert "peak: 1.2G ram / 2.5G vram" in out["peaks"] and out["peaks"].startswith("<div")
    assert 'res-tag">4G ram' in out["mixed"] and "peak: 0.0G ram" in out["mixed"]
    assert "task-copy-btn" in out["btn"] and "copyTaskName" in out["btn"] and "&quot;p&lt;1&gt;&quot;" in out["btn"] and "p<1>" not in out["btn"]
    assert out["sort"] == [[], [], ["sort-desc"]] and out["asc"] == ["sort-asc"]
