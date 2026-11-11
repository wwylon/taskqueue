"""F-254: shared SQLite write-transaction helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE; commit on success; rollback on error or prior open txn.

    Callers must not hold an open transaction across claim/finalize entry points —
    this helper clears a leftover txn (same as claim_and_insert_local_run historically).
    """
    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    else:
        if conn.in_transaction:
            conn.commit()
