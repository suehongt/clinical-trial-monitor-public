"""Brief-staleness semantics contract (简报 stale 误报回归).

The briefing's only staleness statement is per-source data freshness:
``core.ingestion.registry_health`` → ``intelligence._freshness`` → the
briefing page's completeness banner.  These tests pin the corrected
semantics:

    stale ⇔ the library demonstrably lacks recent data from that source
          (data stamp = max(last_successful_sync, MAX(last_crawled_at))
          older than the source's freshness window),

never because of:
  * a no-op enrich that re-verified records without new content
    (recent ``last_crawled_at`` with an old sync cursor);
  * monitor schedule edits, renames, or no-op monitor runs;
  * sync bookkeeping metadata (next_sync_at / updated_at).

and always scoped to the brief's relevant registries: another registry's
staleness must not leak into a scoped brief.

Task regression grid (no-op rerun / real change / schedule edit / display
metadata / rule change / irrelevant event / already-included stamp /
backfilled old timestamp) maps onto the tests below.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.ingestion import registry_health
from core.intelligence import briefing, overview
from db.connection import close_connection, get_connection
from tests.test_server_api import _insert_record

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
STALE_SYNC = "2026-09-21 00:00:00"      # 8 days before NOW → stale band (>72h)
DELAYED_SYNC = "2026-09-27 00:00:00"    # 2.5 days before NOW → delayed band
FRESH_CRAWL = "2026-09-29 09:00:00"     # 3 hours before NOW → fresh band


def _enable(conn: sqlite3.Connection, short: str = "NCT") -> None:
    conn.execute("UPDATE registry_sources SET enabled=1 WHERE short_name=?", (short,))


def _sync(conn: sqlite3.Connection, stamp: str | None, short: str = "NCT",
          failures: int = 0) -> None:
    conn.execute("DELETE FROM sync_status WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name=?)", (short,))
    conn.execute("""INSERT INTO sync_status (source_id, last_successful_sync, consecutive_failures)
                    VALUES ((SELECT source_id FROM registry_sources WHERE short_name=?), ?, ?)""",
                 (short, stamp, failures))


def _health(short: str = "NCT", now: datetime = NOW) -> dict:
    return {r["short_name"]: r for r in registry_health(now)}[short]


def _brief(conn: sqlite3.Connection, *, registry: str | None = None, scope: str = "monitored"):
    data = overview(conn, scope=scope, registry=registry, window="7d", now=NOW)
    return briefing(data)


# ── A. real missed content → stale ────────────────────────────────────────


def test_missed_source_updates_make_brief_stale(test_db):
    """No contact with the source inside the window → stale, with reason."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTSTALE01", "Old trial", crawled=STALE_SYNC)
    _enable(conn)
    _sync(conn, STALE_SYNC)
    conn.commit()
    assert _health()["freshness"] == "stale"
    assert _health()["stale_reason"] == "NO_RECENT_DATA"
    assert _health()["data_as_of"] == STALE_SYNC
    payload = _brief(conn, registry="clinicaltrials_gov", scope="all")
    assert [s["state"] for s in payload["freshness"]["affected"]] == ["stale"]
    assert payload["freshness"]["affected"][0]["reason"] == "NO_RECENT_DATA"


def test_consecutive_sync_failures_report_sync_failures_reason(test_db):
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTSTALE02", "Old trial", crawled=STALE_SYNC)
    _enable(conn)
    _sync(conn, STALE_SYNC, failures=3)
    conn.commit()
    assert _health()["freshness"] == "stale"
    assert _health()["stale_reason"] == "SYNC_FAILURES"


def test_delayed_band_reports_delayed_not_stale(test_db):
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTSTALE03", "Old trial", crawled=DELAYED_SYNC)
    _enable(conn)
    _sync(conn, DELAYED_SYNC)
    conn.commit()
    assert _health()["freshness"] == "delayed"
    assert _health()["stale_reason"] == "NO_RECENT_DATA"


# ── B. no-op rerun / enrich → NOT stale (the false positive this fixes) ───


def test_noop_enrich_with_old_sync_cursor_keeps_brief_fresh(test_db):
    """The discovery-enrich path re-verifies records (touching
    last_crawled_at) without writing the adapter's sync cursor: the library
    demonstrably holds current data, so this must NOT read as stale."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTNOOP01", "Touched trial", crawled=FRESH_CRAWL)
    _enable(conn)
    _sync(conn, STALE_SYNC)  # adapter cursor never advanced
    conn.commit()
    assert _health()["freshness"] == "fresh"
    assert _health()["stale_reason"] == "OK"
    assert _health()["data_as_of"] == FRESH_CRAWL
    assert _brief(conn, registry="clinicaltrials_gov", scope="all")["freshness"]["affected"] == []


def test_noop_monitor_run_never_stales_brief(test_db):
    """A monitor rerun with 0 entered / 0 left / 0 changed leaves the brief fresh."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTQUIET01", "Quiet trial", crawled=FRESH_CRAWL)
    _enable(conn)
    _sync(conn, FRESH_CRAWL)
    mid = conn.execute("INSERT INTO monitors (name) VALUES ('Quiet')").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id, rules_json) VALUES (?,?)", (mid, '{"query": ["quiet"]}'))
    conn.execute("INSERT INTO monitor_trials (monitor_id, trial_id, currently_matches) VALUES (?, 'NCTQUIET01', 1)", (mid,))
    conn.commit()
    before = _brief(conn, registry="clinicaltrials_gov")["freshness"]["affected"]
    # no-op run bookkeeping: run row + last_checked_at bump, nothing else
    conn.execute("""INSERT INTO monitor_runs (monitor_id, started_at, completed_at, status,
                    matched_count, new_count, changed_count, left_count, trigger)
                    VALUES (?, '2026-09-29 10:00:00', '2026-09-29 10:00:05', 'completed', 1, 0, 0, 0, 'manual')""", (mid,))
    conn.execute("UPDATE monitors SET last_checked_at='2026-09-29 10:00:05' WHERE id=?", (mid,))
    conn.commit()
    after = _brief(conn, registry="clinicaltrials_gov")["freshness"]["affected"]
    assert before == [] and after == []


# ── C. schedule / metadata edits → NOT stale ──────────────────────────────


def test_schedule_edit_never_stales_brief(test_db):
    """Changing next_run_at / cadence / sync bookkeeping must not move freshness."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTCAL01", "Scheduled trial", crawled=FRESH_CRAWL)
    _enable(conn)
    _sync(conn, FRESH_CRAWL)
    mid = conn.execute(
        "INSERT INTO monitors (name, schedule_enabled, schedule_frequency, next_run_at) "
        "VALUES ('Cal', 1, 'daily', '2026-09-30 08:00:00')").lastrowid
    conn.commit()
    before = _health()
    conn.execute("""UPDATE monitors SET next_run_at='2026-10-01 20:00:00',
                    schedule_frequency='weekly', updated_at='2026-09-29 11:00:00' WHERE id=?""", (mid,))
    conn.execute("UPDATE sync_status SET next_sync_at='2026-10-05 00:00:00', updated_at='2026-09-29 11:00:00' WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name='NCT')")
    conn.commit()
    after = _health()
    assert (after["freshness"], after["stale_reason"], after["data_as_of"]) == \
           (before["freshness"], before["stale_reason"], before["data_as_of"]) == ("fresh", "OK", FRESH_CRAWL)
    assert mid  # monitor edits happened; freshness is independent of them


def test_monitor_display_metadata_never_stales_brief(test_db):
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTMETA01", "Meta trial", crawled=FRESH_CRAWL)
    _enable(conn)
    _sync(conn, FRESH_CRAWL)
    conn.commit()
    before = _health()
    conn.execute("UPDATE monitors SET name='Renamed', description='new words' WHERE 1=1")
    conn.commit()
    after = _health()
    assert (after["freshness"], after["stale_reason"]) == (before["freshness"], before["stale_reason"])


def test_schedule_edit_survives_service_restart(test_client_stale_semantics):
    """关键验收: 改时间 → 保存 → 重启服务 → 重载简报,brief 仍 fresh。"""
    client, conn_path = test_client_stale_semantics
    created = client.post("/api/monitors", json={"name": "Restart cal", "rules": {"query": ["quiet trial"]}}).json()
    mid = created["id"]
    affected_before = client.get("/api/intelligence/briefing?scope=all&registry=clinicaltrials_gov").json()["freshness"]["affected"]
    assert affected_before == []
    patched = client.patch(f"/api/monitors/{mid}", json={"next_run_at": "2026-10-01 20:00", "schedule_enabled": True})
    assert patched.status_code == 200
    # "restart": drop the pooled connection and rebuild the app on the same DB
    close_connection()
    from server.app import create_app
    from fastapi.testclient import TestClient
    client2 = TestClient(create_app())
    reloaded = client2.get("/api/intelligence/briefing?scope=all&registry=clinicaltrials_gov").json()["freshness"]
    assert reloaded["affected"] == affected_before == []
    assert reloaded["complete"] is True


# ── D. scope / rule change evaluation ─────────────────────────────────────


def test_disabled_source_excluded_from_affected_with_reason(test_db):
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTDIS01", "Disabled source trial", crawled=STALE_SYNC)
    _sync(conn, STALE_SYNC)
    conn.commit()  # NCT disabled by default in the schema fixture
    row = _health()
    assert row["freshness"] == "disabled" and row["stale_reason"] == "SOURCE_DISABLED"
    assert _brief(conn, registry="clinicaltrials_gov", scope="all")["freshness"]["affected"] == []


def test_scoped_brief_ignores_other_registry_staleness(test_db):
    """Brief scoped to NCT must not go stale because ChiCTR has no fresh data."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTSCOPE01", "NCT trial", crawled=FRESH_CRAWL)
    _insert_record(conn, "ChiCTR", "ChiCTRSCOPE01", "ChiCTR trial", crawled=STALE_SYNC)
    _enable(conn)
    _enable(conn, "ChiCTR")
    _sync(conn, FRESH_CRAWL)
    _sync(conn, STALE_SYNC, short="ChiCTR")
    conn.commit()
    scoped = _brief(conn, registry="clinicaltrials_gov", scope="all")["freshness"]["affected"]
    everything = _brief(conn, registry="all", scope="all")["freshness"]["affected"]
    assert scoped == []
    assert [(s["name"], s["state"], s["reason"]) for s in everything] == \
           [("中国临床试验注册中心", "stale", "NO_RECENT_DATA")]


# ── high-water-mark behaviour of the data stamp ───────────────────────────


def test_sync_cursor_alone_is_a_valid_high_water_mark(test_db):
    """Already-included data: stamp inside the window → fresh (task test 7)."""
    conn = get_connection()
    _enable(conn)
    _sync(conn, FRESH_CRAWL)  # no crawled records at all
    conn.commit()
    assert _health()["freshness"] == "fresh"
    assert _health()["data_as_of"] == FRESH_CRAWL


def test_backfilled_old_crawl_cannot_lower_or_fake_the_stamp(test_db):
    """A record inserted later with an older crawl timestamp can never move
    the high-water stamp backwards — and metadata bumps can't fake it."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTNEW01", "Recently verified", crawled=FRESH_CRAWL)
    _enable(conn)
    _sync(conn, STALE_SYNC)
    conn.commit()
    assert _health()["data_as_of"] == FRESH_CRAWL
    # backfill: created now, timestamp claims January
    _insert_record(conn, "NCT", "NCTBACK01", "Backfilled old stamp", crawled="2026-01-10 08:00:00")
    conn.commit()
    assert _health()["data_as_of"] == FRESH_CRAWL
    # bookkeeping-only bumps never manufacture freshness
    conn.execute("UPDATE sync_status SET updated_at='2026-09-29 11:59:00' WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name='NCT')")
    conn.commit()
    assert _health()["data_as_of"] == FRESH_CRAWL


def test_staleness_is_derived_not_persisted(test_db):
    """Stale is a runtime derivation: without persistence, restoring the
    data stamp restores freshness immediately (no lingering stale flag)."""
    conn = get_connection()
    _insert_record(conn, "NCT", "NCTDER01", "Derivation trial", crawled=STALE_SYNC)
    _enable(conn)
    _sync(conn, STALE_SYNC)
    conn.commit()
    assert _health()["freshness"] == "stale"
    # the enrich path touches the record: same rows, newer stamp → fresh
    conn.execute("UPDATE registry_records SET last_crawled_at=? WHERE source_trial_id='NCTDER01'", (FRESH_CRAWL,))
    conn.commit()
    assert _health()["freshness"] == "fresh"
    assert _health()["stale_reason"] == "OK"


def test_registry_health_unknown_when_never_synced_and_empty(test_db):
    conn = get_connection()
    _enable(conn)
    _sync(conn, None)
    conn.commit()
    assert _health()["freshness"] == "unknown"
    assert _health()["stale_reason"] == "NO_DATA_SYNCED"
    assert _health()["data_as_of"] is None


# ── API-level fixture (mirrors test_server_api's client setup) ────────────


@pytest.fixture
def test_client_stale_semantics(test_db):
    from config import CONFIG
    import ct_report.query as qmod
    qmod.DB_PATH = CONFIG.db.path
    conn = get_connection()
    # This API path intentionally uses the real request clock. Keep the
    # fixture fresh relative to execution so the restart assertion cannot
    # expire on a later calendar date; fixed-clock unit cases above continue
    # to use FRESH_CRAWL and NOW for exact threshold coverage.
    api_fresh_crawl = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _insert_record(conn, "NCT", "NCTQUIET01", "Quiet trial", crawled=api_fresh_crawl)
    _enable(conn)
    _sync(conn, api_fresh_crawl)
    conn.commit()
    from fastapi.testclient import TestClient
    from server.app import create_app
    yield TestClient(create_app()), CONFIG.db.path
