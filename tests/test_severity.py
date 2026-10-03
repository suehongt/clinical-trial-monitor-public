"""Tests for the central change-severity classifier (core/severity.py),
the v10 schema migration and the detect_and_save_events wiring.

Directive §40C: Recruiting → Completed, primary endpoint modified,
enrollment +25%, email changed, whitespace-only.
"""
from __future__ import annotations

import json

import pytest

from core.severity import (
    CRITICAL,
    IMPORTANT,
    MINOR,
    NORMAL,
    classify_change,
    classify_event_row,
)


# ═══════════════════════════════════════════════════════════════════════
#  Classifier — pure rules (directive §40C cases)
# ═══════════════════════════════════════════════════════════════════════


class TestStatusTransitions:
    def test_recruiting_to_completed_is_important(self):
        r = classify_change("status_id", "2", "5", "status",
                            old_status_label="Recruiting",
                            new_status_label="Completed")
        assert r.severity == IMPORTANT
        assert r.change_type == "status_transition"

    def test_recruiting_to_active_not_recruiting_is_important(self):
        r = classify_change("status_id", "2", "4", "status",
                            old_status_label="Recruiting",
                            new_status_label="Active, not recruiting")
        assert r.severity == IMPORTANT

    @pytest.mark.parametrize("label", ["Terminated", "Withdrawn", "Suspended",
                                       "终止", "已暂停"])
    def test_terminal_status_is_critical(self, label):
        r = classify_change("status_id", "2", "9", "status",
                            old_status_label="Recruiting", new_status_label=label)
        assert r.severity == CRITICAL

    def test_terminal_terminal_stays_critical(self):
        r = classify_change("status_id", "8", "6", "status",
                            old_status_label="Suspended",
                            new_status_label="Terminated")
        assert r.severity == CRITICAL

    def test_without_labels_falls_back_to_important(self):
        # bare FK values and no label map — must never crash or over-claim
        r = classify_change("status_id", "2", "5", "status")
        assert r.severity == IMPORTANT


class TestEndpointsAndSponsors:
    def test_primary_endpoint_modified_is_critical(self):
        r = classify_change(
            "primary_endpoint",
            "30-day MACE", "90-day cardiovascular death or rehospitalization",
            "endpoint")
        assert r.severity == CRITICAL
        assert r.change_type == "modified"

    def test_secondary_endpoint_is_normal(self):
        r = classify_change("secondary_endpoints", json.dumps(["A"]),
                            json.dumps(["A", "B"]), "endpoint")
        assert r.severity == NORMAL
        assert r.change_type == "list_item_added"

    def test_sponsor_change_is_critical(self):
        r = classify_change("sponsors", json.dumps(["AstraZeneca"]),
                            json.dumps(["Novartis"]), "sponsor")
        assert r.severity == CRITICAL


class TestEnrollment:
    def test_material_increase_is_important(self):
        r = classify_change("enrollment", "380", "520", "enrollment")
        assert r.severity == IMPORTANT
        assert r.change_type == "numeric_increase"

    def test_enrollment_plus_25_percent(self):
        r = classify_change("enrollment", "400", "500", "enrollment")
        assert r.severity == IMPORTANT

    def test_small_enrollment_change_is_normal(self):
        r = classify_change("enrollment", "500", "510", "enrollment")
        assert r.severity == NORMAL

    def test_decrease_is_numeric_decrease(self):
        r = classify_change("enrollment", "520", "380", "enrollment")
        assert r.change_type == "numeric_decrease"
        assert r.severity == IMPORTANT

    def test_non_numeric_enrollment_still_important(self):
        r = classify_change("enrollment", None, "200", "enrollment")
        assert r.severity == IMPORTANT
        assert r.change_type == "added"


class TestDatesAndLists:
    def test_primary_completion_date_shift_is_important(self):
        r = classify_change("primary_completion_date", "2026-12-01",
                            "2027-06-01", "date")
        assert r.severity == IMPORTANT
        assert r.change_type == "date_shift"

    def test_start_date_is_normal(self):
        r = classify_change("start_date", "2026-01", "2026-03", "date")
        assert r.severity == NORMAL

    def test_country_added_is_important(self):
        r = classify_change("countries", json.dumps(["China", "United States"]),
                            json.dumps(["China", "United States", "Germany"]),
                            "location")
        assert r.severity == IMPORTANT
        assert r.change_type == "list_item_added"

    def test_country_removed(self):
        r = classify_change("countries", json.dumps(["China", "Germany"]),
                            json.dumps(["China"]), "location")
        assert r.change_type == "list_item_removed"

    def test_unknown_field_uses_category_fallback(self):
        r = classify_change("some_new_field", "x", "y", "sponsor")
        assert r.severity == CRITICAL
        r2 = classify_change("some_new_field", "x", "y", "other")
        assert r2.severity == NORMAL


class TestMinorNoise:
    def test_last_updated_at_source_is_minor(self):
        r = classify_change("last_updated_at_source", "2026-08-01",
                            "2026-09-16", "date")
        assert r.severity == MINOR

    def test_contact_change_is_minor(self):
        r = classify_change("contacts", json.dumps(["a@x.com"]),
                            json.dumps(["b@x.com"]), "other")
        assert r.severity == MINOR

    def test_scores_are_ordered(self):
        crit = classify_change("primary_endpoint", "a", "b").importance_score
        imp = classify_change("countries", '["A"]', '["A","B"]').importance_score
        norm = classify_change("secondary_endpoints", '["A"]', '["B"]').importance_score
        minor = classify_change("contacts", '["a"]', '["b"]').importance_score
        assert crit > imp > norm > minor


def test_classify_event_row_resolves_status_labels():
    labels = {"2": "Recruiting", "8": "Terminated"}
    r = classify_event_row(
        {"field_name": "status_id", "old_value": "2", "new_value": "8",
         "change_category": "status"},
        status_labels=labels,
    )
    assert r.severity == CRITICAL


# ═══════════════════════════════════════════════════════════════════════
#  Integration — v10 migration backfill + save-time wiring
# ═══════════════════════════════════════════════════════════════════════


def _seed_two_versions(test_db=None, status_from="Recruiting",
                       status_to="Terminated", enrollment_from=380,
                       enrollment_to=520):
    """Insert source + two record versions; return (v1_id, v2_id)."""
    from db.connection import get_connection

    conn = get_connection()
    src = conn.execute(
        "INSERT INTO registry_sources (short_name, full_name, enabled) "
        "VALUES ('TST', 'Test registry', 1)").lastrowid
    s_from = conn.execute(
        "SELECT status_type_id FROM status_types WHERE label = ?",
        (status_from,)).fetchone()["status_type_id"]
    s_to = conn.execute(
        "SELECT status_type_id FROM status_types WHERE label = ?",
        (status_to,)).fetchone()["status_type_id"]

    def _ins(status_id, enrollment, version):
        cur = conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, status_id, enrollment,
                is_latest, version_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (src, "TST001", "Test trial", status_id, enrollment,
             1 if version == 2 else 0, version))
        return cur.lastrowid

    v1 = _ins(s_from, enrollment_from, 1)
    v2 = _ins(s_to, enrollment_to, 2)
    conn.commit()
    return v1, v2, src


def test_save_path_writes_severity(test_db):
    from core.change_detection import detect_and_save_events
    from db.connection import get_connection

    v1, v2, src = _seed_two_versions()
    n = detect_and_save_events(v2, v1, src, "TST001")
    assert n >= 2  # status + enrollment at minimum

    conn = get_connection()
    rows = {r["field_name"]: dict(r) for r in conn.execute(
        "SELECT field_name, severity, change_type FROM trial_events").fetchall()}
    assert rows["status_id"]["severity"] == "critical"
    assert rows["status_id"]["change_type"] == "status_transition"
    assert rows["enrollment"]["severity"] == "important"
    assert rows["enrollment"]["change_type"] == "numeric_increase"


def test_v10_backfill_classifies_historic_rows(test_db):
    """Events written before v10 (severity NULL) get classified by the
    central rules on migration re-run — simulate by NULLing + re-running."""
    from core.change_detection import detect_and_save_events
    from db.connection import get_connection
    from db.schema import _migrate_v10

    v1, v2, src = _seed_two_versions()
    detect_and_save_events(v2, v1, src, "TST001")

    conn = get_connection()
    conn.execute("UPDATE trial_events SET severity = NULL, change_type = NULL, "
                 "importance_score = NULL")
    conn.commit()

    _migrate_v10(conn)

    rows = {r["field_name"]: dict(r) for r in conn.execute(
        "SELECT field_name, severity FROM trial_events").fetchall()}
    assert rows["status_id"]["severity"] == "critical"
    assert rows["enrollment"]["severity"] == "important"
    # idempotent second run changes nothing
    _migrate_v10(conn)
    again = conn.execute(
        "SELECT count(*) FROM trial_events WHERE severity IS NULL").fetchone()[0]
    assert again == 0


def test_schema_version_is_v10_on_fresh_db(test_db):
    from db.connection import get_connection

    version = get_connection().execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version >= 10
