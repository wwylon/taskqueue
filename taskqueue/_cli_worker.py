"""Worker and capacity CLI commands — extracted from cli.py."""
from __future__ import annotations
import os
import sys
from pathlib import Path
from taskqueue._cli_helpers import _db

def cmd_capacity(home: Path) -> None:
    from taskqueue.capacity import build_capacity
    from taskqueue.db import open_db
    db = open_db(_db(home))
    cap = build_capacity(db)
    db.close()
    m = cap["master"]
    print(f"protocol={cap['protocol']}")
    print(
        f"master: cpu_free={m['cpu_cores_free']}/{m['cpu_cores_total']} "
        f"ram_free={m['ram_gib_free']}/{m['ram_gib_total']} GiB "
        f"gpus={m['gpus']}"
    )
    for w in cap["workers"]:
        print(
            f"worker {w['worker_id']}: {w['status']} "
            f"cpu_free={w['cpu_cores_free']}/{w['cpu_cores_total']} "
            f"ram_free={w['ram_gib_free']}"
        )
    print(f"running_reservations: {len(cap['running_reservations'])}")
    for r in cap["running_reservations"][:20]:
        print(f"  {r['project']}/{r['task_id']} cpu={r['cpu_cores']} gpu={r['gpu_vram_gib']}")



def cmd_worker_list(home: Path) -> int:
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No database found.")
        return 1
    conn = open_db(db)
    workers = conn.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
    conn.close()

    if not workers:
        print("No workers registered yet.")
        return 0

    from taskqueue import __version__ as _mv
    from taskqueue.db import SCHEMA_VERSION as _ms
    print(f"{'Worker ID':<20} {'OS':<10} {'Arch':<10} {'RAM':<8} {'Cores':<6} {'VRAM':<8} {'Status':<10} {'Version':<18} {'Last Seen':<20}")
    print("-" * 110)
    for w in workers:
        ver = w['version'] or '—'
        ws = w['schema_version']
        mismatch = ''
        if w['version']:
            try:
                wt = tuple(int(x) for x in w['version'].split('.')[:3])
                mt = tuple(int(x) for x in _mv.split('.')[:3])
                if wt < mt:
                    mismatch = ' !'
            except Exception:
                pass
        if ws is not None and int(ws) < _ms:
            mismatch = ' !!'
        ver_str = f"{ver} (s{ws or '?'}){mismatch}"
        print(f"{w['worker_id']:<20} {w['os']:<10} {w['arch']:<10} {w['ram_gib']:<8.1f} {w['cpu_cores']:<6} {w['gpu_vram_gib']:<8.1f} {w['status']:<10} {ver_str:<18} {w['last_seen'][:19]}")
    return 0



def cmd_worker_sync(home: Path, worker_id: str, *, master_url: str | None = None, auth_token: str | None = None) -> int:
    """W7.8: ask master to queue a git pull + uv sync on a worker."""
    from taskqueue.db import read_config
    from taskqueue.master_client import post_json

    if not master_url:
        master_url = f"http://127.0.0.1:{int(read_config(home).get('web_port', 8765))}"

    token = auth_token or os.environ.get("TQ_AUTH_TOKEN", "")
    try:
        result = post_json(master_url, f"/api/worker/sync/{worker_id}", token=token)
        if result.get("ok"):
            print(f"Sync queued for worker '{worker_id}'. Will run on next heartbeat (~30s).")
            return 0
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"Failed to contact master at {master_url}: {e}", file=sys.stderr)
        return 1



def cmd_worker_start(
    home: Path,
    master_url: str,
    worker_id: str,
    *,
    auth_token: str | None = None,
) -> None:
    from taskqueue.worker import run_worker
    run_worker(home, master_url, worker_id, auth_token=auth_token)



def cmd_worker_setup(
    home: Path,
    master_url: str,
    worker_id: str | None = None,
    *,
    auth_token: str | None = None,
) -> None:
    from taskqueue.worker import setup_worker
    setup_worker(home, master_url, worker_id, auth_token=auth_token)


def cmd_worker_config_master(
    home: Path,
    *,
    host: str,
    out: Path | None = None,
    remote_only: bool = False,
    port: int | None = None,
) -> int:
    """Emit a join YAML for transferring to a worker machine."""
    from taskqueue.db import get_config, open_db, set_config
    from taskqueue.worker_join import build_join_payload, write_join_file
    import secrets

    host = (host or "").strip()
    if not host:
        print("Usage: tq worker config-master --host <reachable-host> [--out path]", file=sys.stderr)
        return 1
    if host.startswith("http://") or host.startswith("https://"):
        print("--host should be a hostname or IP (no scheme). Example: 100.64.0.1", file=sys.stderr)
        return 1

    db = _db(home)
    home.mkdir(parents=True, exist_ok=True)
    conn = open_db(db)
    try:
        cfg = get_config(conn)
        bind_host = str(cfg.get("bind_host") or "127.0.0.1")
        web_port = int(port if port is not None else cfg.get("web_port", 8765))
        auth_token = str(cfg.get("auth_token") or "").strip()
        if not auth_token:
            auth_token = secrets.token_urlsafe(32)
            set_config(conn, "auth_token", auth_token)
            print("[config-master] generated auth_token (restart daemon if already running)")
    finally:
        conn.close()

    if bind_host in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[config-master] WARNING: bind_host={bind_host!r} — remote workers cannot "
            f"reach the API. Run: tq config set bind_host=0.0.0.0 && tq restart",
            file=sys.stderr,
        )

    master_url = f"http://{host}:{web_port}"
    payload = build_join_payload(
        home, master_url=master_url, auth_token=auth_token, remote_only=remote_only
    )
    out_path = out or (home / "worker-join.yaml")
    write_join_file(out_path, payload)
    print(f"Wrote join file: {out_path}")
    print(f"master_url: {master_url}")
    print(f"projects:   {len(payload.get('projects') or {})}")
    print("Transfer this file securely to the worker, then:")
    print(f"  tq worker setup --from {out_path.name} --worker-id <id>")
    print("(Join file contains auth_token — treat as a secret.)")
    return 0


def cmd_worker_setup_from_join(
    home: Path,
    join_path: Path,
    worker_id: str | None = None,
) -> int:
    """Bootstrap a worker from a join YAML produced by config-master."""
    from taskqueue.worker_join import load_join_file

    try:
        join = load_join_file(join_path)
    except ValueError as e:
        print(f"Invalid join file: {e}", file=sys.stderr)
        return 1
    host_part = join["master_url"].split("://", 1)[-1].split("/")[0]
    if host_part.startswith("127.0.0.1") or host_part.startswith("localhost"):
        print(
            f"[setup] WARNING: master_url points at loopback ({join['master_url']}) "
            "— only works if this process is on the master machine.",
            file=sys.stderr,
        )
    cmd_worker_setup(
        home,
        join["master_url"],
        worker_id,
        auth_token=join["auth_token"],
    )
    return 0



def cmd_worker_remove(home: Path, worker_id: str) -> int:
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No database found.")
        return 1
    conn = open_db(db)
    removed = conn.execute("DELETE FROM workers WHERE worker_id = ?", (worker_id,)).rowcount
    conn.commit()
    conn.close()
    if not removed:
        print(f"Worker '{worker_id}' not found.")
        return 1
    print(f"Deregistered worker '{worker_id}' from coordinator database.")
    return 0



def cmd_worker_build_wheel(home: Path) -> int:
    """W7.5: Build the taskqueue wheel and place it at <home>/worker.whl for distribution."""
    import hashlib
    import shutil
    import subprocess
    import sys
    import tempfile
    from pathlib import Path as _Path

    src_root = _Path(__file__).parent.parent
    pyproject = src_root / "pyproject.toml"
    if not pyproject.is_file():
        print(f"Error: pyproject.toml not found at {src_root}. Cannot build wheel from source.", file=sys.stderr)
        print("Alternative: manually place a pre-built .whl at:", home / "worker.whl")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = _Path(tmp)
        print(f"Building wheel from {src_root} …")
        # Try uv build first, then python -m build.
        built = False
        for cmd in (
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(src_root)],
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(src_root)],
        ):
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                built = True
                break
            print(f"  {cmd[0]}: {result.stderr.strip()[:200]}", file=sys.stderr)

        if not built:
            print("Error: wheel build failed. Install 'build' or 'uv' and retry.", file=sys.stderr)
            return 1

        wheels = sorted(tmp_path.glob("*.whl"))
        if not wheels:
            print("Error: no .whl file produced.", file=sys.stderr)
            return 1

        built_wheel = wheels[-1]
        dest = home / "worker.whl"
        home.mkdir(parents=True, exist_ok=True)

        # Backup previous wheel.
        if dest.is_file():
            shutil.copy2(dest, home / "worker_prev.whl")

        shutil.copy2(built_wheel, dest)

    sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()
    print(f"Wheel saved:  {dest}")
    print(f"SHA-256:      {sha256}")
    print(f"Size:         {dest.stat().st_size / 1024:.1f} KiB")
    print("Workers will auto-upgrade on next registration cycle.")
    return 0


def cmd_fair_share(home: Path) -> int:
    """R9: Print decayed fair-share usage per project."""
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    import time
    conn = open_db(db)
    # F-047: use correct column names (gpu_seconds / cpu_seconds, not gpu_seconds_consumed)
    rows = conn.execute(
        "SELECT project, gpu_seconds, cpu_seconds, updated_at "
        "FROM project_usage ORDER BY gpu_seconds DESC"
    ).fetchall()
    conn.close()
    if not rows:
        print("No usage data recorded yet.")
        return 0
    now = int(time.time())
    print(f"{'Project':<30} {'GPU-hours':>10} {'CPU-hours':>10} {'Last updated':>20}")
    print("-" * 74)
    for r in rows:
        gpu_h = float(r["gpu_seconds"]) / 3600.0
        cpu_h = float(r["cpu_seconds"]) / 3600.0
        age = now - int(r["updated_at"] or 0)
        if age < 3600:
            when = f"{age//60}m ago"
        elif age < 86400:
            when = f"{age//3600}h ago"
        else:
            when = f"{age//86400}d ago"
        print(f"  {r['project']:<28} {gpu_h:>10.2f} {cpu_h:>10.2f} {when:>20}")
    return 0


def cmd_worker_rotate_credential(
    home: Path,
    *,
    master_url: str | None = None,
    auth_token: str | None = None,
) -> int:
    """C1: generate a new per-worker token and rotate it on the master."""
    import hashlib
    import secrets
    from taskqueue._cli_helpers import _db
    from taskqueue.db import open_db, get_config

    worker_yaml_p = home / "worker.yaml"
    if not worker_yaml_p.is_file():
        print("No worker.yaml found. Run 'tq worker setup' first.", file=sys.stderr)
        return 1
    try:
        import yaml
        wconf = yaml.safe_load(worker_yaml_p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"Failed to read worker.yaml: {e}", file=sys.stderr)
        return 1

    worker_id = str(wconf.get("worker_id") or "").strip()
    if not worker_id:
        print("worker.yaml has no worker_id.", file=sys.stderr)
        return 1
    old_token = str(wconf.get("worker_token") or "").strip() or None
    old_hash = hashlib.sha256(old_token.encode()).hexdigest() if old_token else None

    new_token = secrets.token_hex(32)
    new_hash = hashlib.sha256(new_token.encode()).hexdigest()

    if not master_url:
        db = _db(home)
        if db.exists():
            conn = open_db(db)
            cfg = get_config(conn)
            conn.close()
            port = int(cfg.get("web_port", 8765))
            master_url = f"http://127.0.0.1:{port}"
        else:
            master_url = str(wconf.get("master_url") or "http://127.0.0.1:8765")

    from taskqueue.master_client import post_json

    token = auth_token or old_token or os.environ.get("TQ_AUTH_TOKEN", "")
    try:
        result = post_json(master_url, "/api/worker/rotate-credential", {
            "worker_id": worker_id,
            "old_credential_hash": old_hash,
            "new_credential_hash": new_hash,
        }, token=token)
    except Exception as e:
        print(f"Failed to contact master: {e}", file=sys.stderr)
        return 1
    if not result.get("ok"):
        print(f"Master rejected rotation: {result.get('error', 'unknown')}", file=sys.stderr)
        return 1
    wconf["worker_token"] = new_token
    try:
        import yaml as _yaml2
        _tmp_yaml = worker_yaml_p.with_name(worker_yaml_p.name + ".tmp")
        _tmp_yaml.write_text(_yaml2.dump(wconf, default_flow_style=False), encoding="utf-8")
        os.replace(_tmp_yaml, worker_yaml_p)
    except Exception as e:
        print(f"Failed to write worker.yaml: {e}", file=sys.stderr)
        return 1
    print(f"Credential rotated successfully for worker '{worker_id}'.")
    return 0


def cmd_worker_set_trust(
    home: Path,
    worker_id: str,
    tier: str,
    *,
    master_url: str | None = None,
    auth_token: str | None = None,
) -> int:
    """C2: set the trust tier for a worker (admin command; requires master DB access)."""
    from taskqueue._cli_helpers import _db
    from taskqueue.db import open_db

    _VALID_TIERS = {"trusted", "lab", "spot", "untrusted"}
    tier = tier.strip().lower()
    if tier not in _VALID_TIERS:
        print(f"Invalid tier {tier!r}. Must be one of: {sorted(_VALID_TIERS)}", file=sys.stderr)
        return 1
    db = _db(home)
    if not db.exists():
        print("No queue DB found.", file=sys.stderr)
        return 1
    conn = open_db(db)
    try:
        row = conn.execute("SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        if row is None:
            print(f"Worker '{worker_id}' not found.", file=sys.stderr)
            return 1
        conn.execute("UPDATE workers SET trust = ? WHERE worker_id = ?", (tier, worker_id))
        conn.commit()
    finally:
        conn.close()
    print(f"Worker '{worker_id}' trust tier set to '{tier}'.")
    return 0


