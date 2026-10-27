"""Master→worker join-file bootstrap (config-master / setup --from).

The join YAML is a secret (contains auth_token). Transfer out-of-band.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from taskqueue.config import PROTOCOL_VERSION, load_projects, registry_path


JOIN_SCHEMA_VERSION = 1


def build_join_payload(
    home: Path,
    *,
    master_url: str,
    auth_token: str,
    remote_only: bool = False,
) -> dict[str, Any]:
    """Build the join dict written by ``tq worker config-master``.

    Includes every registered project that has a ``git_url`` (needed for clone).
    With ``remote_only=True``, only projects with ``remote: true`` in projects.yaml.
    """
    master_url = master_url.rstrip("/")
    projects_out: dict[str, dict[str, Any]] = {}
    try:
        projects = load_projects(home)
    except Exception:
        projects = {}
    for name, proj in projects.items():
        if not proj.git_url:
            continue
        is_remote = getattr(proj, "remote", False) or _project_remote_raw(home, name)
        if remote_only and not is_remote:
            continue
        entry: dict[str, Any] = {"git_url": proj.git_url}
        if is_remote:
            entry["remote"] = True
        projects_out[name] = entry
    return {
        "schema": JOIN_SCHEMA_VERSION,
        "protocol": PROTOCOL_VERSION,
        "master_url": master_url,
        "auth_token": auth_token,
        "projects": projects_out,
    }


def _project_remote_raw(home: Path, name: str) -> bool:
    import yaml

    path = registry_path(home)
    if not path.is_file():
        return False
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = (raw.get("projects") or {}).get(name) or {}
        return bool(cfg.get("remote"))
    except Exception:
        return False


def load_join_file(path: Path) -> dict[str, Any]:
    """Load and validate a join YAML. Raises ValueError on bad input."""
    import yaml

    if not path.is_file():
        raise ValueError(f"join file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("join file must be a YAML mapping")
    master_url = str(raw.get("master_url") or "").strip().rstrip("/")
    auth_token = str(raw.get("auth_token") or "").strip()
    if not master_url:
        raise ValueError("join file missing master_url")
    if not master_url.startswith(("http://", "https://")):
        raise ValueError(f"master_url must be http(s): {master_url!r}")
    if "127.0.0.1" in master_url or "localhost" in master_url.split("://", 1)[-1].split("/")[0]:
        # Warn via caller; still allow same-host dogfood
        pass
    if not auth_token:
        raise ValueError("join file missing auth_token")
    projects = raw.get("projects") or {}
    if projects is not None and not isinstance(projects, dict):
        raise ValueError("join file projects must be a mapping")
    return {
        "schema": int(raw.get("schema") or JOIN_SCHEMA_VERSION),
        "protocol": int(raw.get("protocol") or PROTOCOL_VERSION),
        "master_url": master_url,
        "auth_token": auth_token,
        "projects": projects if isinstance(projects, dict) else {},
    }


def write_join_file(path: Path, payload: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
