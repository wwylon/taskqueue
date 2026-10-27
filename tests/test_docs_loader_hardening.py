import json

from taskqueue import docs_loader as dl


def test_inline_md_no_double_escape():
    out = dl.inline_md("[x](https://e.com/?a=1&b=2)")
    assert "&amp;amp;" not in out
    assert 'href="https://e.com/?a=1&amp;b=2"' in out


def test_inline_md_drops_javascript_href():
    out = dl.inline_md("[x](javascript:alert(1))")
    assert "javascript" not in out
    assert "<a" not in out
    assert "x" in out


def test_manifest_cached_and_invalidated(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    first = dl.load_manifest(p)
    assert dl.load_manifest(p) is first
    p.write_text(json.dumps({"a": 2, "pad": "xx"}), encoding="utf-8")
    assert dl.load_manifest(p)["a"] == 2
