"""
Database connection management — thin wrapper around sqlite3 with WAL mode.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

from config import CONFIG, ensure_dirs


_local = threading.local()


def _get_raw_connection() -> sqlite3.Connection:
    """Get or create a thread-local connection."""
    db_path = str(CONFIG.db.path)
    if not hasattr(_local, "conn") or _local.conn is None:
        ensure_dirs()  # db/ must exist before sqlite3.connect
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        for pragma, value in CONFIG.db.pragmas.items():
            conn.execute(f"PRAGMA {pragma}={value};")
        _local.conn = conn
    return _local.conn


def get_connection() -> sqlite3.Connection:
    """Get a thread-local read-write connection."""
    return _get_raw_connection()


def close_connection() -> None:
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


@contextmanager
def transaction() -> Generator[sqlite3.Connection, None, None]:
    """Context manager that commits on success, rolls back on exception."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def table_exists(name: str) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone()[0] > 0
