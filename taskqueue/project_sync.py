"""Allowlisted master→worker project file sync (lazy fingerprint + deltas).

No idle file watcher. Workers compare fingerprints before a remote run and
fetch only changed allowlisted paths. ``tq resync`` forces a re-diff.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

# Always excluded even if a push glob would match.
HARD_EXCLUDE_DIR_NAMES = frozenset({
    ".venv",
    "out",
    "bin",
    "bin2",
    "__pycache__",
    ".git",
    "node_modules",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "logs",
    "logs_tests",
    "logs_tests_agent",
})
HARD_EXCLUDE_SUFFIXES = (".pdb", ".obj", ".ilk", ".exp")


def _posix(rel: str) -> str:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def path_is_hard_excluded(rel_posix: str) -> bool:
    parts = PurePosixPath(rel_posix).parts
    if any(p in HARD_EXCLUDE_DIR_NAMES for p in parts):
        return True
    lower = rel_posix.lower()
    return any(lower.endswith(suf) for suf in HARD_EXCLUDE_SUFFIXES)


def path_matches_allowlist(rel_posix: str, patterns: Iterable[str]) -> bool:
    rel = _posix(rel_posix)
    if path_is_hard_excluded(rel):
        return False
    for pat in patterns:
        p = _posix(str(pat))
        if not p:
            continue
        if fnmatch.fnmatch(rel, p):
            return True
        # directory prefix: "src/**" already covered; also allow "src" → src/**
        if p.endswith("/**"):
            prefix = p[:-3]
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
        elif "/" not in p.rstrip("*") and fnmatch.fnmatch(PurePosixPath(rel).name, p):
            return True
    return False


def iter_allowlisted_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    """Return files under root matching push patterns (hard excludes applied)."""
    pats = [str(p) for p in patterns if str(p).strip()]
    if not pats or not root.is_dir():
        return []
    out: list[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # prune hard-excluded dirs in-place
        dirnames[:] = [d for d in dirnames if d not in HARD_EXCLUDE_DIR_NAMES]
        base = Path(dirpath)
        for name in filenames:
            fp = base / name
            try:
                rel = _posix(str(fp.relative_to(root)))
            except ValueError:
                continue
            if path_matches_allowlist(rel, pats):
                out.append(fp)
    out.sort(key=lambda p: _posix(str(p.relative_to(root))))
    return out


def file_entry(root: Path, path: Path) -> dict[str, Any]:
    st = path.stat()
    rel = _posix(str(path.relative_to(root.resolve())))
    return {
        "path": rel,
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
    }


def build_manifest(root: Path, patterns: Iterable[str]) -> list[dict[str, Any]]:
    root = root.resolve()
    return [file_entry(root, p) for p in iter_allowlisted_files(root, patterns)]


def fingerprint_from_manifest(entries: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(e["path"].encode("utf-8"))
        h.update(b"\0")
        h.update(str(e["size"]).encode("ascii"))
        h.update(b"\0")
        h.update(str(e["mtime_ns"]).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def compute_fingerprint(root: Path, patterns: Iterable[str]) -> tuple[str, list[dict[str, Any]]]:
    manifest = build_manifest(root, patterns)
    return fingerprint_from_manifest(manifest), manifest


def resolve_allowed_file(root: Path, rel: str, patterns: Iterable[str]) -> Path | None:
    """Return absolute path if rel is under root and allowlisted; else None."""
    rel_p = _posix(rel)
    if ".." in PurePosixPath(rel_p).parts or rel_p.startswith("/"):
        return None
    if not path_matches_allowlist(rel_p, patterns):
        return None
    root = root.resolve()
    candidate = (root / rel_p).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def worker_sync_state_path(home: Path, project: str) -> Path:
    return home / "sync_state" / f"{project}.json"


def load_worker_sync_state(home: Path, project: str) -> dict[str, Any]:
    path = worker_sync_state_path(home, project)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_worker_sync_state(home: Path, project: str, state: dict[str, Any]) -> None:
    path = worker_sync_state_path(home, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def sync_meta_path(home: Path) -> Path:
    return home / "sync_meta.json"


def _load_sync_meta(home: Path) -> dict[str, Any]:
    path = sync_meta_path(home)
    if not path.is_file():
        return {"generations": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"generations": {}}
    if not isinstance(raw, dict):
        return {"generations": {}}
    gens = raw.get("generations")
    if not isinstance(gens, dict):
        raw["generations"] = {}
    return raw


def _save_sync_meta(home: Path, meta: dict[str, Any]) -> None:
    path = sync_meta_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")


def bump_sync_generation(home: Path, project: str) -> int:
    """Invalidate worker fingerprint short-circuit; next sync re-diffs."""
    meta = _load_sync_meta(home)
    gens = meta.setdefault("generations", {})
    try:
        cur = int(gens.get(project) or 0)
    except (TypeError, ValueError):
        cur = 0
    nxt = cur + 1
    gens[project] = nxt
    _save_sync_meta(home, meta)
    return nxt


def get_sync_generation(home: Path, project: str) -> int:
    meta = _load_sync_meta(home)
    try:
        return int((meta.get("generations") or {}).get(project) or 0)
    except (TypeError, ValueError):
        return 0


def diff_manifest(
    have: list[dict[str, Any]],
    want: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Entries in want that differ from have by path→(size,mtime_ns)."""
    have_map = {e["path"]: (e.get("size"), e.get("mtime_ns")) for e in have}
    return [
        e for e in want
        if have_map.get(e["path"]) != (e.get("size"), e.get("mtime_ns"))
    ]


def pull_from_master(
    *,
    home: Path,
    master_url: str,
    project: str,
    project_root: Path,
    auth_headers: dict[str, str],
    force: bool = False,
) -> dict[str, Any]:
    """Pull allowlisted deltas from master onto ``project_root``.

    Returns a summary dict: ``{ok, skipped, fetched, fingerprint, error?}``.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    master_url = master_url.rstrip("/")
    project_root.mkdir(parents=True, exist_ok=True)

    def _get_json(url: str, timeout: float = 60.0) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=auth_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        fp_resp = _get_json(f"{master_url}/api/sync/fingerprint/{urllib.parse.quote(project)}")
    except Exception as e:
        return {"ok": False, "skipped": False, "fetched": 0, "error": f"fingerprint: {e}"}

    patterns = list(fp_resp.get("push") or [])
    remote_fp = str(fp_resp.get("fingerprint") or "")
    generation = int(fp_resp.get("generation") or 0)
    if fp_resp.get("error") and not patterns:
        return {
            "ok": True,
            "skipped": True,
            "fetched": 0,
            "fingerprint": "",
            "error": str(fp_resp.get("error")),
        }

    state = load_worker_sync_state(home, project)
    if (
        not force
        and state.get("fingerprint") == remote_fp
        and int(state.get("generation") or 0) == generation
        and remote_fp
    ):
        return {
            "ok": True,
            "skipped": True,
            "fetched": 0,
            "fingerprint": remote_fp,
            "generation": generation,
        }

    try:
        man_resp = _get_json(f"{master_url}/api/sync/manifest/{urllib.parse.quote(project)}")
    except Exception as e:
        return {"ok": False, "skipped": False, "fetched": 0, "error": f"manifest: {e}"}

    want = list(man_resp.get("files") or [])
    remote_fp = str(man_resp.get("fingerprint") or remote_fp)
    generation = int(man_resp.get("generation") or generation)
    if not patterns and want:
        # reconstruct patterns from fingerprint push if manifest omitted them
        patterns = list(fp_resp.get("push") or [])

    have = build_manifest(project_root, patterns) if patterns else []
    missing = diff_manifest(have, want)
    fetched = 0
    for entry in missing:
        rel = str(entry["path"])
        url = (
            f"{master_url}/api/sync/file/{urllib.parse.quote(project)}/"
            f"{urllib.parse.quote(rel, safe='/')}"
        )
        dest = project_root / rel.replace("/", os.sep)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers=auth_headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            tmp = dest.with_suffix(dest.suffix + ".tqpartial")
            tmp.write_bytes(data)
            # preserve mtime when possible
            os.replace(tmp, dest)
            try:
                mtime_ns = int(entry.get("mtime_ns") or 0)
                if mtime_ns > 0:
                    os.utime(dest, ns=(mtime_ns, mtime_ns))
            except Exception:
                pass
            fetched += 1
        except Exception as e:
            return {
                "ok": False,
                "skipped": False,
                "fetched": fetched,
                "error": f"fetch {rel}: {e}",
            }

    save_worker_sync_state(
        home,
        project,
        {
            "fingerprint": remote_fp,
            "generation": generation,
            "file_count": len(want),
            "last_fetched": fetched,
        },
    )
    return {
        "ok": True,
        "skipped": False,
        "fetched": fetched,
        "fingerprint": remote_fp,
        "generation": generation,
    }
