"""Schema v8 migration tests: digest_state + refresh_queue.

v8 adds two tables for the push/refresh closed loop:
  - digest_state      — single-row watermark for the daily digest push
  - refresh_queue     — Chinese-source re-verification work queue
Both are plain CREATE IF NOT EXISTS via ALL_TABLES (v7 pattern); these
tests pin existence, constraints (incl. the partial unique index), and
migration idempotency.
"""
from __future__ import annotations

import sqlite3

import pytest

from db.connection import get_connection
from db.schema import create_schema


def _version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"]


def test_v8_tables_exist_and_version_recorded(test_db):
    conn = get_connection()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"digest_state", "refresh_queue"} <= names
    assert _version(conn) >= 8


def test_create_schema_idempotent(test_db):
    create_schema()
    create_schema()
    conn = get_connection()
    assert _version(conn) >= 8
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM schema_version WHERE version = 8").fetchone()
    assert rows["n"] == 1


def test_digest_state_single_row_watermark(test_db):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO digest_state (id, last_event_id, last_run_at) "
        "VALUES (1, 42, datetime('now'))")
    row = conn.execute(
        "SELECT last_event_id FROM digest_state WHERE id = 1").fetchone()
    assert row["last_event_id"] == 42
    # REPLACE keeps it a single row
    conn.execute(
        "INSERT OR REPLACE INTO digest_state (id, last_event_id, last_run_at) "
        "VALUES (1, 100, datetime('now'))")
    count = conn.execute("SELECT COUNT(*) AS n FROM digest_state").fetchone()
    assert count["n"] == 1


def _chictr_sid(conn) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name='ChiCTR'"
    ).fetchone()["source_id"]


def test_refresh_queue_defaults(test_db):
    conn = get_connection()
    sid = _chictr_sid(conn)
    conn.execute(
        "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
        "VALUES (?, 'ChiCTR2600128415', 1)", (sid,))
    row = conn.execute("SELECT * FROM refresh_queue").fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["queued_at"]
    assert row["done_at"] is None


def test_refresh_queue_partial_unique_blocks_concurrent_pending(test_db):
    """同一试验同时至多一条 pending；done 行不占用唯一性（可重复复查）。"""
    conn = get_connection()
    sid = _chictr_sid(conn)
    conn.execute(
        "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
        "VALUES (?, 'ChiCTR2600128415', 1)", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
            "VALUES (?, 'ChiCTR2600128415', 1)", (sid,))

    # done 后允许再次入队（生命周期内多次复查）
    conn.execute("UPDATE refresh_queue SET state='done', "
                 "done_at=datetime('now') WHERE source_trial_id='ChiCTR2600128415'")
    conn.execute(
        "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
        "VALUES (?, 'ChiCTR2600128415', 1)", (sid,))
    states = [r["state"] for r in conn.execute(
        "SELECT state FROM refresh_queue "
        "WHERE source_trial_id='ChiCTR2600128415' ORDER BY refresh_id")]
    assert states == ["done", "pending"]


def test_refresh_queue_partial_index_exists(test_db):
    conn = get_connection()
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_refresh_queue_pending'").fetchone()
    assert idx is not None
    assert "WHERE state = 'pending'" in idx["sql"]
