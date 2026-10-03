"""Offline, verified SQLite restore for a trusted single-node deployment."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from core.integrity import audit, open_readonly, startup_check
from db.backup import backup_database


def verify_database(path: str | Path) -> dict:
    with closing(open_readonly(path)) as conn:
        state = startup_check(conn)
        result = audit(conn)
    if result["summary"]["critical"] or result["summary"]["error"]:
        raise RuntimeError(f"database failed integrity audit: {result['summary']}")
    return {**state, "integrity": result["status"]}


def restore_database(source: str | Path, target: str | Path, *, offline: bool = False) -> Path:
    """Validate first, retain a safety backup, then atomically replace target.

    Caller must stop the server, schedulers and collectors. There is no safe
    way to prove every external process is quiescent from inside SQLite.
    """
    if not offline:
        raise RuntimeError("restore requires offline=True after stopping all database writers")
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError("backup and target paths must differ")
    verify_database(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # An exclusive lock catches ordinary active connections. A process that
        # opens the DB later still requires the documented offline boundary.
        with sqlite3.connect(str(target), timeout=0) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            conn.rollback()
        backup_database(str(target), str(target.parent / "restore-safety"),
                        keep=20, label="before-restore")
        with sqlite3.connect(str(target), timeout=0) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    fd, temp_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=target.parent)
    os.close(fd)
    staged = Path(temp_name)
    try:
        with closing(open_readonly(source)) as src, sqlite3.connect(str(staged)) as dst:
            src.backup(dst)
        verify_database(staged)
        os.replace(staged, target)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        return target
    finally:
        staged.unlink(missing_ok=True)
