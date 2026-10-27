"""Tests for taskqueue.docs_loader."""

from __future__ import annotations

import json

import pytest

from taskqueue.docs_loader import (
    build_help_payload,
    expand_topic_includes,
    extract_error_labels,
    load_manifest,
    load_topic,
    markdown_to_html,
    validate_manifest,
    write_generated_artifacts,
)


def test_manifest_valid():
    assert validate_manifest() == []


def test_load_block_project_topic():
    t = load_topic("block-project")
    assert t["id"] == "block-project"
    assert "Block project" in t["title"]
    assert "freeze" in t["body_md"].lower()
    assert "<strong>" in t["body_html"]


def test_page_topic_per_tab():
    manifest = load_manifest()
    for tab in ("queue", "dashboard", "history", "workers", "groups", "deps", "resources", "usage"):
        pid = f"page-{tab}"
        assert pid in manifest["topics"]


def test_help_payload_queue_tab():
    p = build_help_payload("queue", "en")
    assert p["page"]["id"] == "page-queue"
    assert len(p["general"]) >= 5
    assert any(r["id"] == "errors" for r in p["references"])


def test_topic_include_in_guide():
    md = "{{topic:block-project}}"
    html = expand_topic_includes(md)
    assert 'id="block-project"' in html
    assert "<h2>Block project</h2>" in html
    assert "freeze" in html.lower()


def test_markdown_table():
    html = markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_markdown_fenced_code():
    html = markdown_to_html("## Install\n\n```powershell\nuv sync\ntq doctor\n```\n")
    assert "<pre><code class=\"language-powershell\">" in html
    assert "uv sync\ntq doctor" in html
    assert "```" not in html
    assert "<h2>Install</h2>" in html


def test_markdown_to_html_basic():
    html = markdown_to_html("## Title\n\nHello **world**.")
    assert "<h2>Title</h2>" in html
    assert "<strong>world</strong>" in html


def test_relative_md_link_rewrites_to_docs_route():
    from taskqueue.docs_loader import _rewrite_help_href

    assert _rewrite_help_href("../task_standards.md") == "/docs/task-standards"
    assert _rewrite_help_href("task_standards.md") == "/docs/task-standards"
    assert _rewrite_help_href("/docs/task-standards") == "/docs/task-standards"
    html = markdown_to_html("See [task standards](../task_standards.md).")
    assert 'href="/docs/task-standards"' in html


def test_schedule_help_links_task_standards():
    t = load_topic("page-schedule")
    assert "/docs/task-standards" in t["body_html"]
    assert "task_standards.md" not in t["body_html"]


def test_help_payload_schedule_read_more_anchor():
    p = build_help_payload("schedule", "en")
    assert p["page"]["id"] == "page-schedule"
    assert p["page"]["read_more"] == "/docs/web-ui#page-schedule"


def test_error_labels_contain_block_deps():
    labels = extract_error_labels()
    assert labels["tq.block.deps"] == "Waiting on dependencies"
    assert "tq.stop.ram" in labels
    assert labels["tq.stop.deps"] == "Stopped — dependency invalidated"


def test_write_generated_artifacts(tmp_path, monkeypatch):
    import taskqueue.docs_loader as dl

    monkeypatch.setattr(dl, "static_dir", lambda: tmp_path)
    paths = write_generated_artifacts()
    bundle = json.loads(paths["help_bundle"].read_text(encoding="utf-8"))
    assert "queue" in bundle["tabs"]
    labels = json.loads(paths["error_labels"].read_text(encoding="utf-8"))
    assert isinstance(labels, dict)
