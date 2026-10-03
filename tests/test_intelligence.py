"""Phase 3G structured intelligence and briefing contracts (offline)."""
from datetime import datetime, timezone

from core.intelligence import briefing, monitor_comparison, overview
from db.connection import get_connection
from tests.test_server_api import _insert_event, _insert_record


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _fixture(conn):
    a = _insert_record(conn, "NCT", "NCTINT001", "Recruitment trial", status="Recruiting",
                       enrollment=150, sponsors='["Exact Sponsor"]', countries='["China"]',
                       crawled="2026-09-20 09:00:00", first_crawled="2026-09-01 00:00:00")
    b = _insert_record(conn, "NCT", "NCTINT002", "New trial", status="Recruiting",
                       sponsors='["Exact Sponsor"]', crawled="2026-09-20 09:00:00",
                       first_crawled="2026-09-19 00:00:00")
    _insert_event(conn, a, "status_id", "Recruiting", "Completed", "status", "2026-09-20 08:00:00")
    _insert_event(conn, a, "enrollment", "100", "150", "enrollment", "2026-09-20 08:10:00")
    _insert_event(conn, a, "primary_endpoint", "old outcome", "new outcome", "endpoint", "2026-09-20 08:20:00")
    _insert_event(conn, a, "countries", '["China"]', '["China","United States"]', "location", "2026-09-20 08:30:00")
    conn.execute("UPDATE trial_events SET severity='critical' WHERE field_name='primary_endpoint'")
    conn.execute("UPDATE trial_events SET severity='important' WHERE field_name='status_id'")
    m1 = conn.execute("INSERT INTO monitors (name) VALUES ('Cardiac')").lastrowid
    m2 = conn.execute("INSERT INTO monitors (name) VALUES ('Device')").lastrowid
    for mid in (m1, m2):
        conn.execute("INSERT INTO monitor_trials (monitor_id,trial_id,currently_matches) VALUES (?,?,1)", (mid, "NCTINT001"))
        for eid in [r[0] for r in conn.execute("SELECT event_id FROM trial_events WHERE record_id=?", (a,))]:
            conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,related_change_event_id,detected_at) VALUES (?,?,'trial_changed',?,'2026-09-20 09:00:00')", (mid, "NCTINT001", eid))
    conn.execute("INSERT INTO monitor_trials (monitor_id,trial_id,currently_matches) VALUES (?,?,1)", (m1, "NCTINT002"))
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,detected_at) VALUES (?,?,'trial_entered','2026-09-20 09:00:00')", (m1, "NCTINT002"))
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,detected_at) VALUES (?,?,'trial_left','2026-09-20 09:30:00')", (m2, "NCTINT002"))
    conn.execute("UPDATE registry_sources SET enabled=1 WHERE short_name='NCT'")
    conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES ((SELECT source_id FROM registry_sources WHERE short_name='NCT'),'2026-09-19 12:00:00')")
    conn.commit()
    return a, b, m1, m2


def test_intelligence_counts_drilldown_and_dedup(test_db):
    conn = get_connection(); _, _, m1, _ = _fixture(conn)
    data = overview(conn, scope="monitored", window="7d", registry="clinicaltrials_gov", now=NOW)
    assert data["counts"]["change_events"] == 4
    assert data["counts"]["changed_trials"] == 1
    assert data["counts"]["new_trials"] == 1
    assert data["counts"]["entered"] == 1 and data["counts"]["left"] == 1
    assert data["counts"]["critical_changes"] == 1
    assert data["status_transitions"][0]["from"] == "Recruiting"
    assert data["status_transitions"][0]["to"] == "Completed"
    assert data["enrollment_changes"][0]["delta"] == 50
    assert data["enrollment_changes"][0]["percent"] == 50.0
    assert data["country_changes"][0]["added"] == ["United States"]
    assert data["outcome_changes"][0]["event_id"]
    assert data["freshness"]["affected"] == []
    assert len(data["trends"]) == 8
    assert sum(bucket["critical"] for bucket in data["trends"]) == 1
    one = overview(conn, scope="monitor", monitor_id=m1, registry="clinicaltrials_gov", now=NOW)
    assert one["counts"]["change_events"] == 4


def test_briefing_uses_exact_structured_counts_and_stale_health(test_db):
    conn = get_connection(); _fixture(conn)
    # No contact with NCT since 09-01: neither a successful sync cursor nor
    # any record re-verified after that date — a genuinely stale source.
    conn.execute("UPDATE sync_status SET last_successful_sync='2026-09-01 00:00:00'")
    conn.execute("UPDATE registry_records SET last_crawled_at='2026-09-01 00:00:00'")
    conn.commit()
    data = overview(conn, scope="all", registry="clinicaltrials_gov", now=NOW)
    payload = briefing(data)
    assert payload["counts"]["change_events"] == 4
    assert "change events 4" in payload["summary"]
    assert payload["freshness"]["affected"][0]["state"] == "stale"
    assert payload["freshness"]["affected"][0]["reason"] == "NO_RECENT_DATA"
    assert next(s for s in payload["sections"] if s["key"] == "priority")["items"][0]["event_id"]


def test_monitor_comparison_keeps_membership_context_separate(test_db):
    conn = get_connection(); _, _, m1, m2 = _fixture(conn)
    rows = {r["monitor_id"]: r for r in monitor_comparison(conn, registry="clinicaltrials_gov", now=NOW)}
    assert rows[m1]["current_trials"] == 2 and rows[m2]["current_trials"] == 1
    assert rows[m1]["entered"] == 1 and rows[m2]["left"] == 1
    assert rows[m1]["critical"] == rows[m2]["critical"] == 1


def test_cross_source_disagreement_uses_confirmed_links_only(test_db):
    conn = get_connection(); nct, _, _, _ = _fixture(conn)
    chi = _insert_record(conn, "ChiCTR", "ChiCTRINT001", "Linked trial", status="Completed", enrollment=200)
    conn.execute("INSERT INTO master_trials (master_trial_id,preferred_title) VALUES ('int-master','Linked trial')")
    for rid in (nct, chi):
        conn.execute("INSERT INTO record_master_map (record_id,master_trial_id,match_method,match_status) VALUES (?,'int-master','identifier','AUTO_CONFIRMED')", (rid,))
    conn.commit()
    data = overview(conn, scope="all", registry="all", now=NOW)
    disagreements = data["cross_source_disagreements"]
    assert disagreements["logical_trials"] == 1
    assert disagreements["fields"]["enrollment"] == 1
    assert disagreements["fields"]["status"] == 1
