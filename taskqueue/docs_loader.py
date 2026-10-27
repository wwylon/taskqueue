"""Load reusable help topics, guides, and error labels from docs/."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Source checkout: <repo>/docs. Installed wheel: docs are force-included as taskqueue/_docs.
_DOCS_ROOT = _REPO_ROOT / "docs"
if not (_DOCS_ROOT / "manifest.json").is_file():
    _DOCS_ROOT = Path(__file__).resolve().parent / "_docs"
_MANIFEST_PATH = _DOCS_ROOT / "manifest.json"

# T-058: ids whose auto-generated .title() capitalisation is wrong.
_TITLE_OVERRIDES: dict[str, str] = {
    "mcp": "MCP",
    "web-ui": "Web UI",
}
_TOPIC_INCLUDE = re.compile(r"\{\{topic:([a-z0-9_-]+)\}\}")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_CODE_TABLE_ROW = re.compile(r"^\|\s*`(tq\.[^`]+)`\s*\|\s*([^|]+)\|")

UI_TABS = (
    "queue", "dashboard", "history", "workers", "groups",
    "deps", "resources", "usage", "tasks", "timeline", "schedule",
    "alerts", "audit", "api", "docs", "config",
)


def docs_root() -> Path:
    return _DOCS_ROOT


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


_MANIFEST_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Load the docs manifest; cached per path and invalidated on mtime/size change."""
    p = path or _MANIFEST_PATH
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
        hit = _MANIFEST_CACHE.get(str(p))
        if hit and (hit[0], hit[1]) == key:
            return hit[2]
    except OSError:
        key = None
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if key is not None:
        _MANIFEST_CACHE[str(p)] = (key[0], key[1], data)
    return data


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text.strip()
    meta = yaml.safe_load(m.group(1)) or {}
    body = text[m.end() :].strip()
    return meta, body


def topic_path(topic_id: str, lang: str = "en", manifest: dict[str, Any] | None = None) -> Path | None:
    """Resolve topic file. Help corpus is English-only; lang is ignored for topics."""
    del lang
    manifest = manifest or load_manifest()
    topics = manifest.get("topics", {})
    if topic_id not in topics:
        return None
    rel = topics[topic_id]["path"]
    return _DOCS_ROOT / rel


def load_topic(topic_id: str, lang: str = "en", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest()
    path = topic_path(topic_id, lang, manifest)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"topic not found: {topic_id} ({lang})")
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    tid = meta.get("id") or topic_id
    title = meta.get("title") or tid.replace("-", " ").title()
    return {
        "id": tid,
        "title": title,
        "summary": meta.get("summary", ""),
        "ui_pages": meta.get("ui_pages") or [],
        "audience": meta.get("audience") or [],
        "guide_anchors": meta.get("guide_anchors") or {},
        "body_md": body,
        "body_html": markdown_to_html(body),
        "path": str(path.relative_to(_REPO_ROOT)).replace("\\", "/"),
    }


def page_topic_id(tab: str) -> str:
    return f"page-{tab}" if tab != "config" else "page-config"


def expand_topic_includes(text: str, lang: str = "en", manifest: dict[str, Any] | None = None) -> str:
    manifest = manifest or load_manifest()

    def repl(match: re.Match[str]) -> str:
        tid = match.group(1)
        topic = load_topic(tid, lang, manifest)
        anchor = (topic.get("guide_anchors") or {}).get("web-ui") or tid
        title = html.escape(topic.get("title") or tid)
        return (
            f'<section id="{html.escape(anchor)}">\n'
            f"<h2>{title}</h2>\n"
            f'{topic["body_html"]}\n'
            f"</section>"
        )

    return _TOPIC_INCLUDE.sub(repl, text)


def markdown_to_html(md: str) -> str:
    """Minimal markdown subset — enough for help topics without extra deps."""
    if not md.strip():
        return ""
    lines = md.splitlines()
    out: list[str] = []
    in_ul = False
    in_table = False
    in_fence = False
    fence_lang = ""
    fence_lines: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{inline_md(' '.join(para))}</p>")
            para = []

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    def flush_fence() -> None:
        nonlocal in_fence, fence_lang, fence_lines
        if not in_fence:
            return
        lang = html.escape(fence_lang) if fence_lang else ""
        cls = f' class="language-{lang}"' if lang else ""
        body = html.escape("\n".join(fence_lines))
        out.append(f"<pre><code{cls}>{body}</code></pre>")
        in_fence = False
        fence_lang = ""
        fence_lines = []

    def is_table_sep(line: str) -> bool:
        return line.strip().startswith("|") and set(line) <= set("|-: ") and "-" in line

    def table_cells(line: str) -> list[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    for raw in lines:
        line = raw.rstrip()
        if in_fence:
            if line.strip().startswith("```"):
                flush_fence()
            else:
                fence_lines.append(raw.rstrip("\n"))
            continue
        if line.strip().startswith("```"):
            flush_para()
            close_ul()
            close_table()
            fence_lang = line.strip()[3:].strip()
            in_fence = True
            fence_lines = []
            continue
        if not line.strip():
            flush_para()
            close_ul()
            close_table()
            continue
        if line.startswith("### "):
            flush_para()
            close_ul()
            close_table()
            out.append(f"<h3>{inline_md(line[4:].strip())}</h3>")
            continue
        if line.startswith("## "):
            flush_para()
            close_ul()
            close_table()
            out.append(f"<h2>{inline_md(line[3:].strip())}</h2>")
            continue
        if line.startswith("# "):
            flush_para()
            close_ul()
            close_table()
            out.append(f"<h1>{inline_md(line[2:].strip())}</h1>")
            continue
        if line.lstrip().startswith("- "):
            flush_para()
            close_table()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline_md(line.lstrip()[2:].strip())}</li>")
            continue
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            flush_para()
            close_ul()
            if is_table_sep(line):
                continue
            cells = table_cells(line)
            if not in_table:
                out.append("<table><thead><tr>")
                out.append("".join(f"<th>{inline_md(c)}</th>" for c in cells))
                out.append("</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in cells) + "</tr>")
            continue
        flush_para()
        close_ul()
        close_table()
        para.append(line.strip())
    if in_fence:
        flush_fence()
    flush_para()
    close_ul()
    close_table()
    return "\n".join(out)


def _rewrite_help_href(href: str, manifest: dict[str, Any] | None = None) -> str:
    """Map repo-relative .md links to in-app /docs/<id> routes."""
    h = (href or "").strip()
    if not h or h.startswith(("http://", "https://", "mailto:", "#", "/api/")):
        return h
    if h.startswith("/docs/"):
        rest = h[len("/docs/") :]
        if rest.endswith(".md"):
            rest = rest[:-3]
        # Prefer manifest reference ids when basename matches.
        base = rest.rsplit("/", 1)[-1].replace("_", "-")
        man = manifest or load_manifest()
        refs = man.get("references") or {}
        if rest in refs or rest.replace("_", "-") in refs:
            return f"/docs/{rest.replace('_', '-')}"
        for rid, rel in refs.items():
            if Path(rel).stem.replace("_", "-") == base or Path(rel).stem == rest:
                return f"/docs/{rid}"
        return f"/docs/{rest.replace('_', '-')}"
    if h.endswith(".md") or ".md#" in h:
        path_part, _, frag = h.partition("#")
        base = Path(path_part).name
        stem = base[:-3] if base.endswith(".md") else base
        man = manifest or load_manifest()
        for rid, rel in (man.get("references") or {}).items():
            if Path(rel).name == base or Path(rel).stem == stem:
                return f"/docs/{rid}" + (f"#{frag}" if frag else "")
        return f"/docs/{stem.replace('_', '-')}" + (f"#{frag}" if frag else "")
    return h


def inline_md(text: str, manifest: dict[str, Any] | None = None) -> str:
    s = html.escape(text)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    def _link(m: re.Match[str]) -> str:
        label = m.group(1)
        # The text was already HTML-escaped above; undo that before rewriting so
        # the final escape is applied exactly once (no "&amp;amp;").
        href = _rewrite_help_href(html.unescape(m.group(2)), manifest)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href) and not href.lower().startswith(
            ("http://", "https://", "mailto:")
        ):
            return label  # javascript:, data:, vbscript: … are never linked
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, s)
    return s


def render_guide(guide_id: str, lang: str = "en", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest()
    guides = manifest.get("guides", {})
    if guide_id not in guides:
        raise FileNotFoundError(f"guide not found: {guide_id}")
    gmeta = guides[guide_id]
    path = _DOCS_ROOT / gmeta["path"]
    if not path.is_file():
        raise FileNotFoundError(f"guide file missing: {path}")
    raw = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(raw) if raw.startswith("---") else ({}, raw)
    expanded = expand_topic_includes(body, lang, manifest)
    title = meta.get("title") or guide_id.replace("-", " ").title()
    # Render remaining markdown outside topic sections
    parts = re.split(r"(<section id=\"[^\"]+\">.*?</section>)", expanded, flags=re.DOTALL)
    html_parts = []
    for part in parts:
        if part.startswith("<section"):
            html_parts.append(part)
        else:
            html_parts.append(markdown_to_html(part))
    content_html = "\n".join(p for p in html_parts if p.strip())
    return {
        "id": guide_id,
        "title": title,
        "audience": gmeta.get("audience", "human"),
        "ui_pages": gmeta.get("ui_pages") or [],
        "html": content_html,
    }


def render_reference(ref_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest()
    refs = manifest.get("references", {})
    if ref_id not in refs:
        raise FileNotFoundError(f"reference not found: {ref_id}")
    path = _DOCS_ROOT / refs[ref_id]
    text = path.read_text(encoding="utf-8")
    _, body = split_frontmatter(text) if text.startswith("---") else ({}, text)
    return {
        "id": ref_id,
        "title": ref_id.replace("-", " ").title(),
        "html": markdown_to_html(body),
    }


def build_help_payload(tab: str = "queue", lang: str = "en", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest()
    ui_lang = (lang or "en").split("-")[0].lower()
    if ui_lang not in ("en", "pl", "zh", "ar"):
        ui_lang = "en"
    # Topic bodies are English-only (docs/help/*.md). lang is echoed for UI chrome / legacy locale fallback.

    page: dict[str, Any] | None = None
    ptid = page_topic_id(tab)
    topics = manifest.get("topics", {})
    if ptid in topics:
        try:
            t = load_topic(ptid, "en", manifest)
            page = {
                "id": t["id"],
                "title": t["title"],
                "html": t["body_html"],
                "read_more": f"/docs/web-ui#{t.get('guide_anchors', {}).get('web-ui', t['id'])}",
            }
        except FileNotFoundError:
            pass

    general: list[dict[str, Any]] = []
    for tid in manifest.get("general_topic_ids", []):
        try:
            t = load_topic(tid, "en", manifest)
            general.append({"id": t["id"], "title": t["title"], "html": t["body_html"]})
        except FileNotFoundError:
            continue

    references = [
        {"id": rid, "title": _TITLE_OVERRIDES.get(rid, rid.replace("-", " ").title()), "url": f"/docs/{rid}"}
        for rid in manifest.get("references", {})
    ]

    guides_meta = []
    for gid, g in manifest.get("guides", {}).items():
        guides_meta.append({
            "id": gid,
            "title": _TITLE_OVERRIDES.get(gid, gid.replace("-", " ").title()),
            "audience": g.get("audience", "human"),
            "url": f"/docs/{gid}",
            "ui_pages": g.get("ui_pages") or [],
        })

    return {
        "tab": tab,
        "lang": ui_lang,
        "page": page,
        "general": general,
        "references": references,
        "guides": guides_meta,
    }


def build_help_bundle(lang: str = "en", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Offline snapshot: help payloads for all primary UI tabs."""
    manifest = manifest or load_manifest()
    tabs = [
        "queue", "dashboard", "history", "workers", "groups", "deps",
        "resources", "usage", "tasks", "timeline", "schedule", "alerts",
        "audit", "api", "config",
    ]
    return {
        "lang": lang,
        "tabs": {t: build_help_payload(t, lang, manifest) for t in tabs},
    }


def extract_error_labels(errors_path: Path | None = None) -> dict[str, str]:
    """Parse docs/errors.md tables into code → short UI label."""
    path = errors_path or (_DOCS_ROOT / "errors.md")
    text = path.read_text(encoding="utf-8")
    labels: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("| `tq."):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        code = cells[0].strip("`")
        if len(cells) >= 3 and cells[1] and not cells[1].startswith("`"):
            # Code | Short label | Meaning
            short = cells[1].strip()
        else:
            # Code | Meaning — derive short label from meaning (first clause)
            meaning = cells[1] if len(cells) == 2 else cells[-1]
            short = meaning.split("—")[0].split("(")[0].strip()
            if len(short) > 48:
                short = short[:45] + "…"
        labels[code] = short
    # Legacy UI labels for codes present in old _ERROR_LABELS but worded better
    overrides = {
        "tq.infra.clock_jump": "Host slept/woke — re-queued",
        "tq.infra.clock_jump_flap": "Clock-jump flap — use tq retry",
        "tq.infra.worker_lost": "Worker lost — lease expired",
        "tq.infra.lease_expired_worker_alive": "Lease expired (worker still online)",
        "tq.infra.hung": "Hung task — re-queued",
        "tq.infra.orphan": "Orphan run — re-queued",
        "tq.stop.ram": "Stopped — host RAM hard limit",
        "tq.stop.vram": "Stopped — GPU VRAM hard limit",
        "tq.stop.deps": "Stopped — dependency invalidated",
        "tq.dep.cancelled": "Cancelled — dependency failed",
        "tq.stop.scavenger": "Suspended — preempted for other work",
        "tq.block.scavenger": "Waiting — capacity contended",
        "tq.block.deps": "Waiting on dependencies",
        "tq.block.resources": "Waiting on resources",
        "tq.block.exclusive": "Waiting on exclusive gate",
        "tq.block.exclusive_project": "Waiting on project exclusive gate",
        "tq.block.frozen": "Project blocked (frozen)",
        "tq.block.paused": "Project paused",
        "tq.block.mutex": "Waiting on named lock",
        "tq.block.requires_paths": "Required paths missing",
        "tq.run.frozen": "No output / idle process",
        "tq.run.cap": "Hit wall-clock cap",
        "tq.run.nonzero": "Non-zero exit",
        "tq.run.preflight": "Preflight failed",
        "tq.run.progress_suspect": "Progress suspect",
    }
    labels.update(overrides)
    return labels


def write_generated_artifacts(manifest: dict[str, Any] | None = None) -> dict[str, Path]:
    manifest = manifest or load_manifest()
    out_dir = static_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle_path = out_dir / "help-bundle.en.json"
    labels_path = out_dir / "error_labels.json"

    bundle_path.write_text(
        json.dumps(build_help_bundle("en", manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    labels_path.write_text(
        json.dumps(extract_error_labels(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"help_bundle": bundle_path, "error_labels": labels_path}


def validate_manifest(manifest: dict[str, Any] | None = None) -> list[str]:
    """Return list of validation errors (empty if ok)."""
    manifest = manifest or load_manifest()
    errors: list[str] = []
    for tid, info in manifest.get("topics", {}).items():
        rel = info.get("path")
        if not rel:
            errors.append(f"topic {tid}: missing path")
            continue
        p = _DOCS_ROOT / rel
        if not p.is_file():
            errors.append(f"topic {tid}: file missing {rel}")
            continue
        meta, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        if meta.get("id") and meta["id"] != tid:
            errors.append(f"topic {tid}: frontmatter id mismatch ({meta['id']})")
    for gid, info in manifest.get("guides", {}).items():
        p = _DOCS_ROOT / info["path"]
        if not p.is_file():
            errors.append(f"guide {gid}: file missing {info['path']}")
    for rid, rel in manifest.get("references", {}).items():
        p = _DOCS_ROOT / rel
        if not p.is_file():
            errors.append(f"reference {rid}: file missing {rel}")
    ui_tabs = {
        "queue", "dashboard", "history", "workers", "groups", "deps",
        "resources", "usage", "tasks", "timeline", "schedule", "alerts",
        "audit", "api", "config",
    }
    topics = manifest.get("topics", {})
    for tab in ui_tabs:
        pid = page_topic_id(tab)
        if pid not in topics:
            errors.append(f"missing page topic: {pid}")
    return errors
