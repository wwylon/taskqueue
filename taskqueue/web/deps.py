"""Shared imports for Handler mixins (avoids circular imports with package __init__)."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from taskqueue import __version__
from taskqueue.auth import extract_request_token
from taskqueue.operator_features import (
    count_firing_rules,
    create_alert,
    create_token,
    delete_alert,
    is_worker_path,
    list_alerts,
    list_audit,
    list_tokens,
    pause_task,
    project_scope_allows,
    resolve_access,
    resolve_token_projects,
    revoke_token,
    schedules as list_schedules,
    scope_allows,
    task_stats,
    timeline,
    update_alert,
    write_audit,
)
from taskqueue.system_alerts import (
    count_system_alert_firing,
    list_system_alerts,
    update_system_alert,
)
from taskqueue.queries import status_counts
from taskqueue.capacity import build_capacity
from taskqueue.config import load_projects, validate_config
from taskqueue.db import (
    SCHEMA_VERSION,
    _PROJECT_LIMIT_FIELDS,
    get_all_project_limits,
    get_config,
    holds_master_lease,
    is_project_frozen,
    open_db,
    set_project_frozen,
    set_config,
    set_project_limit,
)
from taskqueue._db_tasks import (
    check_ownership,
    enable_task,
    get_queued_tasks,
    count_queued_tasks,
    get_running_tasks,
    get_task,
    lock_contention_summary,
    lock_occupancy,
    request_stop_with_control,
    retry_skip_reason,
    retry_task,
    set_task_priority,
    set_task_status,
)
from taskqueue._db_runs import force_task, get_recent_runs
from taskqueue.diagnose import build_diagnose
from taskqueue.notify import notify_log_path
from taskqueue.queries import list_attention
from taskqueue.reports import build_report
from taskqueue.resources import ram_used_gib, task_requires_gpu

from .api_docs import _API_REGISTRY, _build_api_docs_html
from .hardware import _get_hardware_data
from .responses import (
    _AUTH_CFG_TTL,
    _GIT_SHA,
    _PROJECTS_CACHE_TTL,
    _STATIC,
    _UNGROUPED,
    _UPLOAD_CHUNK,
    _apply_deps_mode,
    _attach_lock_waiting_on,
    _attach_log_field,
    _attach_progress_health,
    _auth_cfg_cache,
    _auth_cfg_lock,
    _claim_next_remote_task,
    _get_auth_config,
    _get_cost_response,
    _get_deps_response,
    _get_groups_response,
    _get_history_response,
    _get_queue_response,
    _get_suites_response,
    _get_usage_response,
    _history_row_dict,
    _live_run_dict,
    _projects_cache,
    _projects_cache_ts,
    _projects_lock,
    _queue_row_dict,
    _redact_config,
    _safe_resolve,
    _slim_resources,
    _slim_status_payload,
    _task_dict,
    _wheel_sha_cache,
    relativize_requires_paths,
    stream_request_body_to_file,
)

# Re-export modules commonly used as bare names in methods
__all__ = [n for n in globals() if not n.startswith("__")]
