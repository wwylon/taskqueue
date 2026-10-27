"""Handler mixin (extracted from make_handler)."""
from __future__ import annotations
from taskqueue.jsonutil import json_list

# Broad shared namespace so method bodies keep working with minimal churn.
from .deps import (
    Path,
    _safe_resolve,
    json,
    re,
    stream_request_body_to_file,
    unquote,
)
from taskqueue.auth import finalize_token_matches
from taskqueue.rows import row_get


def _upstream_host_allowed(host: str | None) -> bool:
    """Reject link-local / metadata / unspecified / multicast upstreams (SSRF).

    Loopback and private LAN hosts stay allowed: local model servers are the main use case.
    """
    return _resolve_allowed_upstream_ip(host) is not None


def _resolve_allowed_upstream_ip(host: str | None) -> str | None:
    """Return one validated connect IP for ``host``, or None if the host is forbidden.

    Resolves once and pins that IP so a later DNS rebinding cannot retarget the
    proxy at a link-local/metadata address after the allow-check.
    """
    import ipaddress
    import socket

    if not host:
        return None
    # Literal IP: validate without DNS.
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return None
        return str(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None  # unresolvable: refuse (do not open a blind connect)
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            continue
        return str(ip)
    return None


def _snapshot_path_allowed(norm: str, data_block: dict | None) -> bool:
    """Allow SQLite snapshots only under ``data/`` or declared ``data.inputs`` paths."""
    n = (norm or "").replace("\\", "/").lstrip("./")
    if not n or ".." in n.split("/"):
        return False
    if n == "data" or n.startswith("data/"):
        return True
    if not isinstance(data_block, dict):
        return False
    inputs = data_block.get("inputs")
    if not isinstance(inputs, list):
        return False
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        path_pat = str(inp.get("path") or "").replace("\\", "/").lstrip("./")
        if not path_pat:
            continue
        # Exact file or directory prefix from the input declaration.
        if n == path_pat or n.startswith(path_pat.rstrip("/") + "/"):
            return True
        # Glob-ish: if pattern has no wildcards and is a parent of n.
        if "*" not in path_pat and "?" not in path_pat:
            continue
        from fnmatch import fnmatch

        if fnmatch(n, path_pat) or fnmatch(n, path_pat.rstrip("/") + "/**"):
            return True
    return False

class TransferRoutes:
    def _serve_gzip_artifact(self, project: str, hash_val: str, path_resolver) -> None:
        if not re.fullmatch(r"[0-9a-f]{8,128}", hash_val):
            self.send_error(400)
            return
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", project):
            self.send_error(400)
            return
        f = path_resolver(project, hash_val)
        if not f.is_file():
            self.send_error(404)
            return
        self._serve_file(f, "application/gzip")

    def _serve_bundle(self, project: str, bundle_hash: str) -> None:
        from taskqueue.config import state_home
        self._serve_gzip_artifact(
            project,
            bundle_hash,
            lambda p, h: state_home() / "bundles" / p / f"{h}.tar.gz",
        )

    def _serve_package_overlay(self, project: str, patch_hash: str) -> None:
        """R-009: serve dirty-tree overlay tar for a git_patch package."""
        from taskqueue.config import state_home
        from taskqueue.provision import overlay_path
        self._serve_gzip_artifact(
            project,
            patch_hash,
            lambda p, h: overlay_path(state_home(), p, h),
        )

    def _serve_sync_fingerprint(self, project_name: str) -> None:
        """Allowlisted tree fingerprint for lazy master→worker sync."""
        from taskqueue.config import state_home
        from taskqueue.project_sync import compute_fingerprint, get_sync_generation

        project = self._projects_ref().get(project_name)
        if not project:
            self._send_error_json(404, "unknown project")
            return
        patterns = list(getattr(project, "sync_push", ()) or ())
        if not patterns:
            self._json({
                "project": project_name,
                "fingerprint": "",
                "generation": get_sync_generation(state_home(), project_name),
                "file_count": 0,
                "push": [],
                "remote": bool(getattr(project, "remote", False)),
                "error": "no sync.push allowlist",
            })
            return
        fp, manifest = compute_fingerprint(project.root, patterns)
        self._json({
            "project": project_name,
            "fingerprint": fp,
            "generation": get_sync_generation(state_home(), project_name),
            "file_count": len(manifest),
            "push": patterns,
            "remote": bool(getattr(project, "remote", False)),
        })

    def _serve_sync_manifest(self, project_name: str) -> None:
        from taskqueue.config import state_home
        from taskqueue.project_sync import (
            build_manifest,
            fingerprint_from_manifest,
            get_sync_generation,
        )

        project = self._projects_ref().get(project_name)
        if not project:
            self._send_error_json(404, "unknown project")
            return
        patterns = list(getattr(project, "sync_push", ()) or ())
        if not patterns:
            self._json({"project": project_name, "files": [], "fingerprint": "", "generation": 0})
            return
        manifest = build_manifest(project.root, patterns)
        self._json({
            "project": project_name,
            "files": manifest,
            "fingerprint": fingerprint_from_manifest(manifest),
            "generation": get_sync_generation(state_home(), project_name),
        })

    def _serve_sync_file(self, project_name: str, rel_path: str) -> None:
        from taskqueue.project_sync import resolve_allowed_file

        rel_path = unquote(rel_path)
        project = self._projects_ref().get(project_name)
        if not project:
            self._send_error_json(404, "unknown project")
            return
        patterns = list(getattr(project, "sync_push", ()) or ())
        target = resolve_allowed_file(project.root, rel_path, patterns)
        if target is None:
            self._send_error_json(403, "path not allowlisted or missing")
            return
        # Cap single-file sync (sources should be well under this).
        max_bytes = 256 * 1024 * 1024
        try:
            if target.stat().st_size > max_bytes:
                self._send_error_json(413, f"file exceeds {max_bytes} bytes")
                return
        except OSError:
            self._send_error_json(404, "missing")
            return
        self._serve_file(target, "application/octet-stream")

    def _serve_data_manifest(self, project_name: str) -> None:
        import os
        project = self._projects_ref().get(project_name)
        if not project:
            self.send_error(404)
            return
        data_dir = project.root / "data"
        files = []
        truncated = False
        _MAX_MANIFEST = 10_000
        if data_dir.is_dir():
            for dirpath, _, filenames in os.walk(data_dir):
                for f in filenames:
                    if len(files) >= _MAX_MANIFEST:
                        truncated = True
                        break
                    abs_p = Path(dirpath) / f
                    rel_p = abs_p.relative_to(project.root)
                    try:
                        stat = abs_p.stat()
                        files.append({
                            "path": str(rel_p).replace("\\", "/"),
                            "size": stat.st_size,
                            "mtime": stat.st_mtime
                        })
                    except Exception:
                        pass
                if truncated:
                    break
        self._json({"files": files, "truncated": truncated})

    def _serve_data_file(self, project_name: str, rel_path: str) -> None:
        # F-050: URL-decode rel_path so percent-encoded filenames resolve
        rel_path = unquote(rel_path)
        project = self._projects_ref().get(project_name)
        if not project:
            self.send_error(404)
            return
        try:
            # F-017: resolve() follows symlinks — catches escapes that relative_to alone misses
            target_file = _safe_resolve(project.root, rel_path)
            if target_file is None:
                raise ValueError("path escape")
            # Legacy project route only serves what the manifest lists: <root>/data/**.
            target_file.relative_to((project.root / "data").resolve())
        except ValueError:
            self.send_error(403)
            return
        if not target_file.is_file():
            self.send_error(404)
            return
        self._serve_file(target_file, "application/octet-stream")

    def _content_length(self) -> int:
        """Content-Length as int; malformed or negative values read as 0."""
        try:
            return max(0, int(self.headers.get("Content-Length", 0)))
        except (TypeError, ValueError):
            return 0

    def _norm_transfer_rel(self, rel: str) -> str | None:
        """Normalize a transfer relative path; None if unsafe."""
        raw = (rel or "").replace("\\", "/").strip()
        if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            return None
        parts = Path(raw).parts
        if any(p == ".." for p in parts):
            return None
        return Path(*parts).as_posix() if parts else None

    def _authorize_transfer(
        self,
        project_name: str,
        rel_path: str,
        *,
        direction: str,
    ) -> bool:
        """F-065: require run_id + finalize_token and a matching transfer: path."""
        # F-122: transfer routes require the master lease (same as claim/finalize).
        if not self._require_master_lease():
            return False
        qs = self._query()
        run_id_raw = (qs.get("run_id") or [None])[0]
        # Prefer header (F-121); query still accepted for one-release compat.
        token = self.headers.get("X-TQ-Finalize-Token") or (qs.get("finalize_token") or [None])[0]
        if not run_id_raw or not token:
            self._send_error_json(
                401, "run_id and finalize_token required for transfer"
            )
            return False
        try:
            run_id = int(run_id_raw)
        except (TypeError, ValueError):
            self._send_error_json(400, "Invalid run_id")
            return False

        norm = self._norm_transfer_rel(rel_path)
        if norm is None:
            self._send_error_json(403, "Invalid transfer path")
            return False

        conn = self._rconn()
        run = conn.execute(
            "SELECT project, task_id, status, finalize_token FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            self._send_error_json(404, "Unknown run_id")
            return False
        stored = row_get(run, "finalize_token")
        if not finalize_token_matches(stored, token):
            self._send_error_json(403, "finalize_token mismatch")
            return False
        if run["status"] != "running":
            self._send_error_json(409, "run is not active")
            return False
        if str(run["project"]) != project_name:
            self._send_error_json(403, "project does not match run")
            return False

        task = conn.execute(
            "SELECT transfer FROM tasks WHERE project = ? AND task_id = ?",
            (run["project"], run["task_id"]),
        ).fetchone()
        try:
            specs = json_list((task["transfer"] if task else None))
        except Exception:
            specs = []
        if not isinstance(specs, list):
            specs = []

        allowed: set[str] = set()
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            d = str(spec.get("direction") or "push")
            if direction == "push" and d == "push":
                n = self._norm_transfer_rel(str(spec.get("src") or ""))
                if n:
                    allowed.add(n)
            elif direction == "pull" and d == "pull":
                n = self._norm_transfer_rel(
                    str(spec.get("dst") or spec.get("src") or "")
                )
                if n:
                    allowed.add(n)

        if norm not in allowed:
            self._send_error_json(
                403, f"path not declared in transfer: for this run ({direction})"
            )
            return False
        return True

    def _serve_transfer_file(self, project_name: str, rel_path: str) -> None:
        """W7.6/F-065: serve a declared push-path from the master project root."""
        if not self._authorize_transfer(project_name, rel_path, direction="push"):
            return
        project = self._projects_ref().get(project_name)
        if not project:
            self._send_error_json(404, f"Unknown project: {project_name}")
            return
        target = _safe_resolve(project.root, rel_path)
        if target is None:
            self._send_error_json(403, "Path escapes project root")
            return
        if not target.is_file():
            self._send_error_json(404, f"File not found: {rel_path}")
            return
        self._serve_file(target, "application/octet-stream")

    def _receive_transfer_file(self, project_name: str, rel_path: str) -> None:
        """W7.6/F-065: receive a declared pull-path into the master project root."""
        if not self._authorize_transfer(project_name, rel_path, direction="pull"):
            return
        project = self._projects_ref().get(project_name)
        if not project:
            self._send_error_json(404, f"Unknown project: {project_name}")
            return
        target = _safe_resolve(project.root, rel_path)
        if target is None:
            self._send_error_json(403, "Path escapes project root")
            return
        # Reject writes into the queue dir.
        try:
            target.relative_to(project.queue_dir.resolve())
            self._send_error_json(403, "Writing into queue dir is not allowed")
            return
        except (ValueError, AttributeError):
            pass
        length = self._content_length()
        if length <= 0 or length > self._MAX_UPLOAD_BYTES:
            self._send_error_json(400, "Content-Length missing or exceeds 64 MiB")
            return
        try:
            stream_request_body_to_file(self.rfile, length, target)
        except OSError as exc:
            self._send_error_json(400, f"Upload failed: {exc}")
            return
        self._json({"ok": True, "path": str(target)})

    # W1.5: 64 MB cap on all uploads.
    _MAX_UPLOAD_BYTES = 64 * 1024 * 1024
    _MAX_PROXY_BODY_BYTES = 16 * 1024 * 1024

    # ------------------------------------------------------------------ D1 CAS

    def _serve_cas_blob(self, sha256: str) -> None:
        """D1: serve a CAS blob by content hash (requires valid run token)."""
        from taskqueue.config import state_home
        from taskqueue.data.manifest import cas_blob_path

        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            self._send_error_json(400, "Invalid sha256")
            return

        # Require a valid finalize_token from query string or header
        run_id_raw = (self._query().get("run_id") or [None])[0]
        if not run_id_raw:
            self._send_error_json(401, "run_id and finalize_token required")
            return
        if self._authorize_active_run(run_id_raw) is None:
            return

        blob = cas_blob_path(state_home() / "cas", sha256)
        if not blob.is_file():
            self._send_error_json(404, f"blob not found: {sha256}")
            return
        self._serve_file(blob, "application/octet-stream")

    def _receive_cas_blob(self, run_id_str: str, rel_path: str) -> None:
        """D1: receive an output blob from worker and store in CAS."""
        from taskqueue.config import state_home
        from taskqueue.data.manifest import cas_blob_path, _sha256_file
        import tempfile

        try:
            run_id = int(run_id_str)
        except (TypeError, ValueError):
            self._send_error_json(400, "Invalid run_id")
            return

        # Authorize using finalize_token
        token = self.headers.get("X-TQ-Finalize-Token")
        if not token:
            self._send_error_json(401, "X-TQ-Finalize-Token required")
            return

        conn = self._rconn()
        run = conn.execute(
            "SELECT project, task_id, status, finalize_token FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            self._send_error_json(404, "Unknown run")
            return
        stored = row_get(run, "finalize_token")
        if not finalize_token_matches(stored, token):
            self._send_error_json(403, "finalize_token mismatch")
            return
        if run["status"] != "running":
            self._send_error_json(409, "run is not active")
            return

        # Read body and store
        length = self._content_length()
        if length <= 0:
            self._send_error_json(400, "Content-Length required")
            return
        if length > 512 * 1024 * 1024:  # 512 MiB max
            self._send_error_json(413, "blob too large")
            return

        cas_root = state_home() / "cas"
        cas_root.mkdir(parents=True, exist_ok=True)

        remaining = length
        with tempfile.NamedTemporaryFile(dir=cas_root, delete=False, suffix=".tmp") as tf:
            tmp_path = Path(tf.name)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    tf.write(chunk)
                    remaining -= len(chunk)
            except OSError:
                pass
        if remaining > 0:
            # Truncated upload: never store a partial blob under its own hash.
            tmp_path.unlink(missing_ok=True)
            self._send_error_json(400, "incomplete body")
            return

        try:
            sha256 = _sha256_file(tmp_path)
            dest = cas_blob_path(cas_root, sha256)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.is_file():
                tmp_path.replace(dest)
            else:
                tmp_path.unlink(missing_ok=True)
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            self._send_error_json(500, f"store failed: {e}")
            return

        self._json({"ok": True, "sha256": sha256})

    def _authorize_active_run(self, run_id_str: str):
        """Validate run_id + finalize_token (query or header) for a *running* run.

        Sends the error response and returns None on failure; otherwise the
        ``runs`` row (project, task_id, status, finalize_token).
        """
        # F-122: data/CAS routes require master lease.
        if not self._require_master_lease():
            return None
        try:
            run_id = int(run_id_str)
        except (TypeError, ValueError):
            self._send_error_json(400, "invalid run_id")
            return None
        qs = self._query()
        token = self.headers.get("X-TQ-Finalize-Token") or (qs.get("finalize_token") or [None])[0]
        if not token:
            self._send_error_json(401, "finalize_token required")
            return None
        run = self._rconn().execute(
            "SELECT project, task_id, status, finalize_token FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None or run["status"] != "running":
            self._send_error_json(404, "run not active")
            return None
        stored = row_get(run, "finalize_token")
        if not finalize_token_matches(stored, token):
            self._send_error_json(403, "finalize_token mismatch")
            return None
        return run

    def _serve_data_manifest_for_run(self, run_id_str: str) -> None:
        """D1: return the data.inputs content-addressed manifest for a run's task."""
        from taskqueue.data.manifest import build_manifest, store_blob
        from taskqueue.config import state_home

        try:
            run_id = int(run_id_str)
        except (TypeError, ValueError):
            self._send_error_json(400, "invalid run_id")
            return

        run = self._authorize_active_run(run_id_str)
        if run is None:
            return
        conn = self._rconn()

        project = self._projects_ref().get(run["project"])
        if not project:
            self._send_error_json(404, "project not found")
            return

        task = conn.execute(
            "SELECT data FROM tasks WHERE project = ? AND task_id = ?",
            (run["project"], run["task_id"]),
        ).fetchone()
        data_block = None
        if task:
            try:
                data_block = json.loads(task["data"] or "null")
            except Exception:
                pass

        manifests = []
        if data_block and isinstance(data_block.get("inputs"), list):
            for inp in data_block["inputs"]:
                if not isinstance(inp, dict):
                    continue
                provider = inp.get("provider", "fetch")
                if provider != "fetch":
                    continue
                path_pat = inp.get("path", "")
                max_bytes_str = inp.get("max_bytes")
                max_bytes = None
                if max_bytes_str:
                    try:
                        s = str(max_bytes_str).upper().replace(" ", "")
                        if s.endswith("GIB"):
                            max_bytes = int(float(s[:-3]) * 1024**3)
                        elif s.endswith("MIB"):
                            max_bytes = int(float(s[:-3]) * 1024**2)
                        else:
                            max_bytes = int(s)
                    except Exception:
                        pass
                try:
                    manifest = build_manifest(path_pat, project.root, max_bytes=max_bytes)

                    # Store blobs in CAS so workers can download them
                    cas_root = state_home() / "cas"
                    for entry in manifest:
                        file_path = project.root / entry["path"]
                        if file_path.is_file():
                            try:
                                store_blob(cas_root, file_path)
                            except Exception:
                                pass

                    manifests.append({
                        "mount": inp.get("mount", ""),
                        "readonly": inp.get("readonly", True),
                        "files": manifest,
                    })
                except Exception as e:
                    manifests.append({"mount": inp.get("mount", ""), "error": str(e)})

        self._json({"run_id": run_id, "manifests": manifests})

    # ------------------------------------------------------------------ D2 SQLite snapshot

    def _serve_sqlite_snapshot(self, run_id_str: str, db_rel_path: str) -> None:
        """D2: create and serve a VACUUM INTO snapshot of a SQLite file."""
        from taskqueue.data.manifest import sqlite_snapshot, store_blob
        from taskqueue.config import state_home

        try:
            run_id = int(run_id_str)
        except (TypeError, ValueError):
            self._send_error_json(400, "invalid run_id")
            return

        run = self._authorize_active_run(run_id_str)
        if run is None:
            return

        project = self._projects_ref().get(run["project"])
        if not project:
            self._send_error_json(404, "project not found")
            return

        # Normalize and validate the db path
        norm = self._norm_transfer_rel(db_rel_path)
        if not norm:
            self._send_error_json(403, "invalid db path")
            return

        conn = self._rconn()
        task = conn.execute(
            "SELECT data FROM tasks WHERE project = ? AND task_id = ?",
            (run["project"], run["task_id"]),
        ).fetchone()
        data_block = None
        if task:
            try:
                data_block = json.loads(task["data"] or "null")
            except Exception:
                data_block = None
        if not _snapshot_path_allowed(norm, data_block if isinstance(data_block, dict) else None):
            self._send_error_json(403, "snapshot path not allowlisted (use data/ or data.inputs)")
            return

        db_path = _safe_resolve(project.root, norm)
        if db_path is None or not db_path.is_file():
            self._send_error_json(404, f"db not found: {norm}")
            return

        # Create snapshot
        snaps_dir = state_home() / "snapshots"
        snaps_dir.mkdir(parents=True, exist_ok=True)
        import uuid as _uuid

        # Unique per request (two DBs sharing a basename must not collide) and
        # removed once served so snapshots do not accumulate in the state home.
        snap_path = snaps_dir / f"run{run_id}_{_uuid.uuid4().hex[:8]}_{Path(norm).name}"

        try:
            try:
                sqlite_snapshot(db_path, snap_path)
            except Exception as e:
                self._send_error_json(500, f"snapshot failed: {e}")
                return

            # Store in CAS
            cas_root = state_home() / "cas"
            try:
                store_blob(cas_root, snap_path)
            except Exception:
                pass

            self._serve_file(snap_path, "application/octet-stream")
        finally:
            try:
                snap_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ D3 service proxy

    def _proxy_service_request(self, service_name: str, sub_path: str) -> None:
        """D3: HTTP-proxy broker — verify token and forward to upstream."""
        from taskqueue.data.service_broker import verify_service_token
        import urllib.request as _ureq

        # Get token and run_id from headers
        token = self.headers.get("X-TQ-Service-Token") or \
                self.headers.get("Authorization", "").replace("Bearer ", "")
        run_id_raw = self.headers.get("X-TQ-Run-Id")

        if not token or not run_id_raw:
            self._send_error_json(401, "X-TQ-Service-Token and X-TQ-Run-Id required")
            return

        try:
            run_id = int(run_id_raw)
        except (TypeError, ValueError):
            self._send_error_json(400, "invalid X-TQ-Run-Id")
            return

        method = self.command
        ok, reason = verify_service_token(token, run_id, service_name, method, f"/{sub_path}")
        if not ok:
            self._send_error_json(403, f"service token rejected: {reason}")
            return

        # Look up the upstream URL from the task's data.services
        conn = self._rconn()
        run = conn.execute(
            "SELECT project, task_id, status FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not run or run["status"] != "running":
            self._send_error_json(404, "run not active")
            return

        task = conn.execute(
            "SELECT data FROM tasks WHERE project = ? AND task_id = ?",
            (run["project"], run["task_id"]),
        ).fetchone()

        upstream = None
        if task:
            try:
                data_block = json.loads(task["data"] or "null")
                if data_block and isinstance(data_block.get("services"), list):
                    for svc in data_block["services"]:
                        if isinstance(svc, dict) and svc.get("name") == service_name:
                            upstream = svc.get("upstream")
                            break
            except Exception:
                pass

        if not upstream:
            self._send_error_json(404, f"service {service_name!r} not configured for this task")
            return

        # Forward the request
        import urllib.error as _uerr
        import urllib.parse as _uparse

        _up = _uparse.urlparse(str(upstream))
        if _up.scheme not in ("http", "https"):
            self._send_error_json(502, "upstream scheme not allowed")
            return
        pinned_ip = _resolve_allowed_upstream_ip(_up.hostname)
        if not pinned_ip:
            self._send_error_json(502, "upstream host not allowed")
            return
        length = self._content_length()
        if length > self._MAX_PROXY_BODY_BYTES:
            self._send_error_json(413, "request body too large")
            return
        try:
            body = self.rfile.read(length) if length > 0 else None

            # Connect to the IP we validated; keep the original Host header (anti-rebinding).
            import ipaddress as _ipa

            try:
                _ipa.ip_address(pinned_ip)
                host_for_netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
            except ValueError:
                host_for_netloc = pinned_ip
            netloc = f"{host_for_netloc}:{_up.port}" if _up.port else host_for_netloc
            fwd_path = ((_up.path or "").rstrip("/") + "/" + sub_path.lstrip("/")).lstrip("/")
            fwd_path = "/" + fwd_path
            fwd_url = f"{_up.scheme}://{netloc}{fwd_path}"
            if _up.query:
                fwd_url += "?" + _up.query
            fwd_req = _ureq.Request(fwd_url, data=body, method=method)
            if _up.hostname:
                host_hdr = _up.hostname if not _up.port else f"{_up.hostname}:{_up.port}"
                fwd_req.add_header("Host", host_hdr)
            for header in ("Content-Type", "Accept"):
                val = self.headers.get(header)
                if val:
                    fwd_req.add_header(header, val)

            class _NoRedirect(_ureq.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):  # noqa: D401
                    return None

            opener = _ureq.build_opener(_NoRedirect)
            try:
                fwd_resp = opener.open(fwd_req, timeout=30)
            except _uerr.HTTPError as he:
                fwd_resp = he  # upstream 4xx/5xx is passed through, not turned into 502
            with fwd_resp:
                resp_body = fwd_resp.read(self._MAX_PROXY_BODY_BYTES + 1)
                status = fwd_resp.status if hasattr(fwd_resp, "status") else fwd_resp.code
                ct = fwd_resp.headers.get("Content-Type", "application/octet-stream")
            if len(resp_body) > self._MAX_PROXY_BODY_BYTES:
                self._send_error_json(502, "upstream response too large")
                return

            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self._send_error_json(502, f"upstream error: {e}")

    def _handle_data_upload(self, project_name: str) -> None:
        project = self._projects_ref().get(project_name)
        if not project:
            self.send_error(404)
            return
        rel_path = self.headers.get("X-File-Path")
        if not rel_path:
            self.send_error(400)
            return

        # W1.2: resolve() both sides so `..` traversal can't escape the root.
        try:
            target_file = project.resolve(rel_path).resolve()
            root_resolved = project.root.resolve()
        except Exception:
            self._send_error_json(403, "Invalid file path")
            return
        try:
            target_file.relative_to(root_resolved)
        except ValueError:
            self._send_error_json(403, "Path escapes project root")
            return

        # W1.2: reject writes into the queue dir (YAML-injection attack vector).
        try:
            queue_dir = project.queue_dir.resolve()
            target_file.relative_to(queue_dir)
            self._send_error_json(403, "Writing into the queue directory is not allowed")
            return
        except (ValueError, AttributeError):
            pass  # target is not inside queue_dir — that's the happy path

        # W1.5 / F-073: reject oversized bodies; stream to disk (fixed buffer).
        if self.headers.get("Content-Length") is None:
            # A missing header would read as 0 bytes and replace the target with an empty file.
            self._send_error_json(411, "Content-Length required")
            return
        length = self._content_length()
        if length > self._MAX_UPLOAD_BYTES:
            self._send_error_json(413, f"Body too large (max {self._MAX_UPLOAD_BYTES // (1024*1024)} MiB)")
            return
        try:
            stream_request_body_to_file(self.rfile, length, target_file)
        except OSError as exc:
            self._send_error_json(400, f"Upload failed: {exc}")
            return
        self._json({"ok": True})
