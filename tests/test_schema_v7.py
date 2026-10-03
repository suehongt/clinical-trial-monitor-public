"""Schema v7 migration tests: discovery_cursors + discovery_queue.

The discovery layer (the project design notes)
adds two tables: per-mode incremental watermarks and a decoupled
discovery→enrichment work queue.  Both are plain CREATE IF NOT EXISTS
via ALL_TABLES, so the migration itself only records the version bump —
these tests pin existence, constraints, and idempotency.
"""
from __future__ import annotations

import sqlite3

import pytest

from db.connection import get_connection
from db.schema import create_schema


def _version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"]


def test_v7_tables_exist_and_version_recorded(test_db):
    conn = get_connection()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"discovery_cursors", "discovery_queue"} <= names
    assert _version(conn) >= 7


def test_create_schema_idempotent(test_db):
    create_schema()
    create_schema()
    conn = get_connection()
    assert _version(conn) >= 7
    # exactly one v7 row despite repeated create_schema calls
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM schema_version WHERE version = 7").fetchone()
    assert rows["n"] == 1


def test_discovery_cursors_unique_source_mode(test_db):
    conn = get_connection()
    sid = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name='NCT'"
    ).fetchone()["source_id"]
    conn.execute(
        "INSERT INTO discovery_cursors (source_id, mode, cursor_json) "
        "VALUES (?, 'list_walk', '{}')", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO discovery_cursors (source_id, mode, cursor_json) "
            "VALUES (?, 'list_walk', '{}')", (sid,))


def test_discovery_queue_unique_and_defaults(test_db):
    conn = get_connection()
    sid = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name='ChiCTR'"
    ).fetchone()["source_id"]
    conn.execute(
        "INSERT INTO discovery_queue (source_id, source_trial_id, discovered_via) "
        "VALUES (?, 'ChiCTR2400087654', 'list_walk')", (sid,))
    row = conn.execute(
        "SELECT state, attempts, discovered_at FROM discovery_queue"
    ).fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["discovered_at"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO discovery_queue (source_id, source_trial_id, discovered_via) "
            "VALUES (?, 'ChiCTR2400087654', 'id_probe')", (sid,))


def test_discovery_queue_state_index_exists(test_db):
    conn = get_connection()
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_discovery_queue_state'").fetchone()
    assert idx is not None


def test_title_column_upgrade_guard_for_pre_title_v7_db(tmp_path, monkeypatch):
    """给 v7 首版（无 title 列）建的存量库自动补列（create_schema 幂等守卫）。"""
    import sqlite3

    from config import CONFIG as cfg
    from db.connection import close_connection

    db = tmp_path / "old_v7.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE discovery_queue (
            discovery_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        INTEGER NOT NULL,
            source_trial_id  TEXT    NOT NULL,
            discovered_via   TEXT    NOT NULL,
            discovered_at    TEXT    NOT NULL,
            state            TEXT    NOT NULL,
            attempts         INTEGER NOT NULL,
            last_error       TEXT,
            UNIQUE(source_id, source_trial_id)
        )
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(cfg.db, "path", db)
    close_connection()
    create_schema()
    close_connection()

    conn2 = sqlite3.connect(str(db))
    cols = {r[1] for r in conn2.execute("PRAGMA table_info(discovery_queue)")}
    conn2.close()
    assert "title" in cols
