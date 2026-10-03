"""Repair script tests: restore ICTRP records downgraded by portal stubs.

Regression for the thin-over-full downgrade: ingest_live_entries used to
version the WHO search grid's stub rows over full XML-snapshot records,
flipping is_latest onto the stub and hiding countries/sponsors/conditions
from search and facets.  scripts/repair_portal_downgraded_ictrp.py flips
is_latest back to the newest superseded full row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.repair_portal_downgraded_ictrp import find_downgraded, repair  # noqa: E402


@pytest.fixture
def conn(test_db):
    from db.connection import get_connection
    return get_connection()


def _sid(conn, short: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short,),
    ).fetchone()["source_id"]


def _record(conn, short: str, trial_id: str, *, portal_live: bool,
            is_latest: int, countries: str | None = None,
            title: str = "Trial", raw_payload: str | None = None) -> int:
    if raw_payload is None:
        payload = {"who_ictrp_record": {"trial_id": trial_id}}
        if portal_live:
            payload["who_ictrp_record"]["_portal_live"] = 1
        raw_payload = json.dumps(payload)
    version = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) + 1 FROM registry_records "
        "WHERE source_id = ? AND source_trial_id = ?",
        (_sid(conn, short), trial_id),
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, countries, raw_payload,
            version_number, is_latest)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_sid(conn, short), trial_id, title, countries,
         raw_payload, version, is_latest))
    conn.commit()
    return cur.lastrowid


def _event(conn, record_id: int, field: str, old: str | None,
           new: str | None) -> None:
    conn.execute(
        """INSERT INTO trial_events
           (record_id, field_name, old_value, new_value, event_hash)
           VALUES (?, ?, ?, ?, ?)""",
        (record_id, field, old, new, f"hash-{record_id}-{field}-{old}-{new}"))
    conn.commit()


def test_restores_full_row_over_stub(conn):
    full_id = _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=False,
                      is_latest=0, countries='["China"]',
                      title="Snapshot title")
    stub_id = _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=True,
                      is_latest=1, title="Portal title")

    restored = repair(conn, apply=True)

    assert restored == 1
    row = conn.execute(
        "SELECT record_id, title, countries, is_latest FROM registry_records "
        "WHERE record_id IN (?, ?) ORDER BY record_id", (full_id, stub_id),
    ).fetchall()
    assert row[0]["is_latest"] == 1 and row[0]["countries"] == '["China"]'
    assert row[1]["is_latest"] == 0
    # Supersession pointers stay walkable: stub now points at the restored row.
    stub = conn.execute("SELECT superseded_by FROM registry_records "
                        "WHERE record_id = ?", (stub_id,)).fetchone()
    assert stub["superseded_by"] == full_id
    full = conn.execute("SELECT superseded_by FROM registry_records "
                        "WHERE record_id = ?", (full_id,)).fetchone()
    assert full["superseded_by"] is None


def test_ignores_stub_without_full_row(conn):
    """First-discovery portal hits have nothing to restore — that is the
    designed thin-first path, repaired later by the weekly XML snapshot."""
    _record(conn, "ICTRP", "ChiCTR2600129999", portal_live=True, is_latest=1)
    assert find_downgraded(conn) == []
    assert repair(conn, apply=True) == 0


def test_ignores_non_ictrp_and_full_latest(conn):
    """Full-row latest and other registries are out of scope."""
    _record(conn, "ICTRP", "ChiCTR2600128001", portal_live=False, is_latest=1,
            countries='["China"]')
    _record(conn, "NCT", "NCT09999999", portal_live=True, is_latest=1)
    assert find_downgraded(conn) == []


def test_picks_newest_full_row(conn):
    old_full = _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=False,
                       is_latest=0, countries='["China"]', title="v1")
    _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=False,
            is_latest=0, countries='["China"]', title="v2")
    _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=True, is_latest=1)

    repair(conn, apply=True)

    latest = conn.execute(
        "SELECT record_id, title FROM registry_records "
        "WHERE source_trial_id = ? AND is_latest = 1",
        ("ChiCTR2600128224",)).fetchone()
    assert latest["record_id"] != old_full  # newest superseded full row wins
    assert latest["title"] == "v2"


def test_idempotent(conn):
    _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=False, is_latest=0,
            countries='["China"]')
    _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=True, is_latest=1)
    assert repair(conn, apply=True) == 1
    assert repair(conn, apply=True) == 0
    assert find_downgraded(conn) == []


def test_dry_run_writes_nothing(conn):
    _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=False, is_latest=0,
            countries='["China"]')
    _record(conn, "ICTRP", "ChiCTR2600128224", portal_live=True, is_latest=1,
            title="Portal title")
    assert repair(conn, apply=False) == 0
    latest = conn.execute(
        "SELECT title FROM registry_records WHERE is_latest = 1").fetchone()
    assert latest["title"] == "Portal title"


# ── Version-chain invariant ────────────────────────────────────────────
# The 2026-10-01 incident: the first repair flipped is_latest back but left
# the stub at the higher version_number.  The integrity audit's
# INVALID_LATEST_VERSION rule then flagged 183 errors, and the nightly
# pipeline automation "fixed" them by flipping is_latest back onto the
# stubs — undoing the repair.  The repair must therefore swap version
# numbers so the restored full row carries the max version.


_INVALID_LATEST = """
SELECT count(*) FROM registry_records r
WHERE r.is_latest = 1 AND r.version_number <> (
    SELECT MAX(v.version_number) FROM registry_records v
    WHERE v.source_id = r.source_id AND v.source_trial_id = r.source_trial_id)
"""


def test_swaps_versions_so_latest_is_max(conn):
    full_id = _record(conn, "ICTRP", "ChiCTR2600128300", portal_live=False,
                      is_latest=0, countries='["China"]')   # v1
    stub_id = _record(conn, "ICTRP", "ChiCTR2600128300", portal_live=True,
                      is_latest=1)                          # v2

    assert repair(conn, apply=True) == 1

    versions = {r["record_id"]: r["version_number"] for r in conn.execute(
        "SELECT record_id, version_number FROM registry_records "
        "WHERE source_trial_id = 'ChiCTR2600128300'").fetchall()}
    assert versions[full_id] == 2   # restored full row now the max version
    assert versions[stub_id] == 1
    latest = conn.execute(
        "SELECT version_number FROM registry_records "
        "WHERE source_trial_id = 'ChiCTR2600128300' AND is_latest = 1"
    ).fetchone()
    assert latest["version_number"] == 2
    assert conn.execute(_INVALID_LATEST).fetchone()[0] == 0


def test_phantom_clear_events_deleted(conn):
    """Cleared-field events on portal stubs are diff noise against fields
    the grid never carried — deleted; every other event stays."""
    full_id = _record(conn, "ICTRP", "ChiCTR2600128400", portal_live=False,
                      is_latest=0, countries='["China"]')
    stub_id = _record(conn, "ICTRP", "ChiCTR2600128400", portal_live=True,
                      is_latest=1)
    _event(conn, stub_id, "sponsors", '["Zhongshan Hospital"]', None)  # phantom
    _event(conn, stub_id, "countries", '["China"]', "")                # phantom
    _event(conn, stub_id, "status_id", None, "3")                      # filled: keep
    _event(conn, stub_id, "status_id", "2", "3")                       # real drift: keep
    other_full = _record(conn, "ICTRP", "ChiCTR2600128401", portal_live=False,
                         is_latest=1, countries='["China"]')
    _event(conn, other_full, "sponsors", '["A"]', None)                # non-stub: keep
    _event(conn, full_id, "enrollment", "1120", None)                  # superseded row: keep

    repair(conn, apply=True)

    kept = conn.execute(
        "SELECT record_id, field_name, old_value, new_value FROM trial_events "
        "ORDER BY event_id").fetchall()
    assert len(kept) == 4
    assert (kept[0]["field_name"], kept[0]["old_value"], kept[0]["new_value"]) \
        == ("status_id", None, "3")
    assert (kept[1]["field_name"], kept[1]["old_value"], kept[1]["new_value"]) \
        == ("status_id", "2", "3")
    assert kept[2]["record_id"] == other_full
    assert kept[3]["record_id"] == full_id


def test_idempotent_phantom_cleanup(conn):
    _record(conn, "ICTRP", "ChiCTR2600128500", portal_live=False, is_latest=0,
            countries='["China"]')
    stub = _record(conn, "ICTRP", "ChiCTR2600128500", portal_live=True,
                   is_latest=1)
    _event(conn, stub, "countries", '["China"]', None)
    assert repair(conn, apply=True) == 1
    assert repair(conn, apply=True) == 0
    assert conn.execute(_PHANTOM_COUNT_SQL).fetchone()[0] == 0


_PHANTOM_COUNT_SQL = """
SELECT count(*) FROM trial_events te
JOIN registry_records r ON r.record_id = te.record_id
JOIN registry_sources s ON s.source_id = r.source_id
WHERE s.short_name = 'ICTRP'
  AND json_valid(r.raw_payload)
  AND json_extract(r.raw_payload, '$.who_ictrp_record._portal_live') = 1
  AND COALESCE(te.old_value, '') <> ''
  AND COALESCE(te.new_value, '') = ''
"""


def test_malformed_payload_rows_do_not_crash(conn):
    """Corrupted payloads must be skipped, never raise (the malformed
    full row of ChiCTR2400087372 crashed json_extract scans)."""
    _record(conn, "ICTRP", "ChiCTR2600128600", portal_live=False,
            is_latest=0, countries='["China"]',
            raw_payload='{"who_ictrp_record": {"trial_i')  # truncated JSON
    _record(conn, "ICTRP", "ChiCTR2600128600", portal_live=True, is_latest=1,
            raw_payload='not json at all')

    assert find_downgraded(conn) == []      # unparseable stub: out of scope
    assert repair(conn, apply=True) == 0    # and the scan survives
