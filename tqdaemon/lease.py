"""Master lease: compare-and-swap claim, force claim, standby wait and the renew thread.

Extracted from daemon.py (Wave 4.6); daemon re-exports these names.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tqdaemon._time import _ts
from tqdaemon.db import master_holder_process_dead


class MasterLeaseLockTimeout(Exception):
    """F-082: BEGIN IMMEDIATE timed out — not the same as lease held by another."""


# /api/yield-master: this process must not reclaim the lease it just gave up until a
# standby has had a full lease period to take it (monotonic deadline, 0 = not yielding).
_yield_until = 0.0


def yield_master_lease(conn: sqlite3.Connection, holder: str | None) -> bool:
    """Give up the master lease: expire our row (keep holder/epoch so epochs never go
    backwards) and back off from reclaiming for one ``MASTER_LEASE_TTL_SEC``."""
    from tqdaemon.config import MASTER_LEASE_TTL_SEC

    global _yield_until
    if holder:
        cur = conn.execute("UPDATE master_lease SET expires_at=0 WHERE singleton=1 AND holder=?", (holder,))
    else:
        cur = conn.execute("UPDATE master_lease SET expires_at=0 WHERE singleton=1")
    conn.commit()
    _yield_until = time.monotonic() + MASTER_LEASE_TTL_SEC
    return cur.rowcount > 0


def _yielding() -> bool:
    return _yield_until > time.monotonic()


def _cas_master_lease(
    conn: sqlite3.Connection,
    holder: str,
    ttl: int | None = None,
) -> bool:
    """W7.1/F-058: Atomic compare-and-swap for master lease.

    Returns True if this daemon incarnation now holds the lease (claimed or renewed).
    Uses BEGIN IMMEDIATE so only one writer can race through the check-then-set.
    On takeover of an expired/unowned row, bumps master_lease.epoch.

    Same-host holders whose OS PID is gone are treated as expired so a restart
    does not wait out ``MASTER_LEASE_TTL_SEC`` before binding the web UI.

    F-082: raises ``MasterLeaseLockTimeout`` when BEGIN IMMEDIATE fails (busy
    SQLite), so callers do not log a misleading "held by …" message.
    """
    from tqdaemon.config import MASTER_LEASE_TTL_SEC

    if ttl is None:
        ttl = MASTER_LEASE_TTL_SEC
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        # F-020 / F-082: only a busy/locked database is a lock timeout; anything
        # else (e.g. "cannot start a transaction within a transaction" on a
        # shared connection) is a programming error and must not be masked.
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            raise MasterLeaseLockTimeout(str(exc)) from exc
        raise
    try:
        row = conn.execute("SELECT holder, expires_at, epoch FROM master_lease WHERE singleton=1").fetchone()
        if _yielding() and (row is None or int(row["expires_at"]) <= now):
            conn.rollback()  # we just yielded: leave the free lease to a standby for now
            return False
        if row is None:
            conn.execute(
                "INSERT INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, ?, ?, 1)",
                (holder, now + ttl),
            )
            conn.commit()
            return True
        epoch = int(row["epoch"] or 0)
        expired = int(row["expires_at"]) <= now
        stale_dead = (not expired) and row["holder"] != holder and master_holder_process_dead(str(row["holder"]))
        if expired or stale_dead:
            conn.execute(
                "UPDATE master_lease SET holder=?, expires_at=?, epoch=? WHERE singleton=1",
                (holder, now + ttl, epoch + 1),
            )
            conn.commit()
            return True
        if row["holder"] == holder:
            conn.execute(
                "UPDATE master_lease SET expires_at=? WHERE singleton=1 AND holder=? AND epoch=?",
                (now + ttl, holder, epoch),
            )
            conn.commit()
            return True
        conn.rollback()
        return False
    except MasterLeaseLockTimeout:
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _acquire_master_lease(
    conn: sqlite3.Connection,
    my_instance: str,
    claim_master: bool,
    pre_stop: Any,
) -> None:
    """Force-claim the master lease, or wait (interruptibly) until it is ours."""
    from tqdaemon.config import MASTER_LEASE_TTL_SEC

    if claim_master:
        # --claim-master --force: unconditionally overwrite (caller checked --force)
        now = int(time.time())
        row = conn.execute("SELECT epoch FROM master_lease WHERE singleton=1").fetchone()
        epoch = int(row["epoch"] or 0) + 1 if row else 1
        conn.execute(
            "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, ?, ?, ?)",
            (my_instance, now + MASTER_LEASE_TTL_SEC, epoch),
        )
        conn.commit()
        print(
            f"[queue] {_ts()} master lease force-claimed: holder={my_instance} epoch={epoch}",
            flush=True,
        )
    else:
        _lease_lock_timeouts = 0
        while not pre_stop.is_set():
            try:
                if _cas_master_lease(conn, my_instance):
                    print(
                        f"[queue] {_ts()} master lease acquired: holder={my_instance}",
                        flush=True,
                    )
                    break
                _lease_lock_timeouts = 0
            except MasterLeaseLockTimeout as e:
                _lease_lock_timeouts += 1
                print(
                    f"[queue] {_ts()} master lease: SQLite lock timeout ({e}) — retrying ({_lease_lock_timeouts})",
                    flush=True,
                )
                if _lease_lock_timeouts >= 3:
                    print(
                        f"[queue] {_ts()} hint: SQLite lock starvation — "
                        "another process may be holding queue.db open; check for stuck tq/daemon",
                        file=sys.stderr,
                        flush=True,
                    )
                pre_stop.wait(10)
                continue
            except Exception as e:  # noqa: BLE001 - lease thread guard; the next attempt retries
                print(f"[queue] {_ts()} master lease CAS error: {e}", flush=True)
            row = conn.execute("SELECT holder, expires_at FROM master_lease WHERE singleton=1").fetchone()
            if row:
                rem = int(row["expires_at"]) - int(time.time())
                if master_holder_process_dead(str(row["holder"])):
                    print(
                        f"[queue] {_ts()} master lease held by dead '{row['holder']}' — reclaiming...",
                        flush=True,
                    )
                    time.sleep(0.5)
                    continue
                print(
                    f"[queue] {_ts()} master lease held by '{row['holder']}' (expires in {rem}s), waiting...",
                    flush=True,
                )
            pre_stop.wait(10)


def _start_lease_renew_thread(
    db_path: Path,
    my_instance: str,
    lease_lost: threading.Event,
    is_shutdown: Callable[[], bool],
) -> threading.Thread:
    """F-074: renew master_lease on a dedicated thread so a blocked scheduler
    tick (heavy sync / SQLite busy) cannot expire fencing and 503 workers.
    """
    from tqdaemon.config import MASTER_LEASE_RENEW_SEC, MASTER_LEASE_TTL_SEC

    def _master_lease_renew_loop() -> None:
        from tqdaemon.db import open_write_conn as _owc

        while not is_shutdown():
            time.sleep(MASTER_LEASE_RENEW_SEC)
            if is_shutdown():
                break
            try:
                wconn = _owc(db_path, timeout=5.0)
                try:
                    cur = wconn.execute(
                        "UPDATE master_lease SET expires_at=? WHERE singleton=1 AND holder=?",
                        (int(time.time()) + MASTER_LEASE_TTL_SEC, my_instance),
                    )
                    wconn.commit()
                    if cur.rowcount == 0:
                        row = wconn.execute("SELECT holder, expires_at FROM master_lease WHERE singleton=1").fetchone()
                        holder_now = row["holder"] if row else "unknown"
                        print(
                            f"[queue] {_ts()} WARN: master lease lost to '{holder_now}' "
                            f"(renew thread); halting remote admit until reclaim",
                            file=sys.stderr,
                            flush=True,
                        )
                        lease_lost.set()
                    else:
                        lease_lost.clear()
                finally:
                    wconn.close()
            except Exception as exc:  # noqa: BLE001 - lease renew thread guard; the next attempt retries
                print(
                    f"[queue] {_ts()} warn: master lease renew thread error: {exc}",
                    file=sys.stderr,
                )

    thread = threading.Thread(target=_master_lease_renew_loop, name="tq-master-lease", daemon=True)
    thread.start()
    return thread


class MasterLease:
    """This daemon's hold on the master lease: claim (or wait) at boot, then keep it renewed.

    ``lost`` is set by the renew thread when another holder took the lease; the scheduler tick
    fences DB writes on it and clears it after a successful reclaim. Thin wrapper over the
    module functions above (tests patch those).
    """

    def __init__(self, db_path: Path, instance: str) -> None:
        self.db_path = db_path
        self.instance = instance
        self.lost = threading.Event()
        self._renew_thread: threading.Thread | None = None

    def acquire(self, conn: sqlite3.Connection, *, force: bool, pre_stop: Any) -> None:
        """Force-claim (``--claim-master --force``) or wait, interruptibly, until the lease is ours."""
        _acquire_master_lease(conn, self.instance, force, pre_stop)

    def start_renewal(self, is_shutdown: Callable[[], bool]) -> threading.Thread:
        self._renew_thread = _start_lease_renew_thread(self.db_path, self.instance, self.lost, is_shutdown)
        return self._renew_thread
