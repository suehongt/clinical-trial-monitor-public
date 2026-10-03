"""Tests for the change detection module — pure functions & DB integration."""
from __future__ import annotations

import json

from core.change_detection import (
    compare_values,
    compute_event_hash,
    detect_changes,
    detect_and_save_events,
)


# ═══════════════════════════════════════════════════════════════════════
#  compare_values — pure function (no DB needed)
# ═══════════════════════════════════════════════════════════════════════


class TestCompareValues:
    def test_identical_values(self):
        assert compare_values("hello", "hello") is False

    def test_different_values(self):
        assert compare_values("foo", "bar") is True

    def test_both_none(self):
        assert compare_values(None, None) is False

    def test_none_and_empty(self):
        assert compare_values(None, "") is False
        assert compare_values("", None) is False

    def test_none_and_nonempty(self):
        assert compare_values(None, "foo") is True
        assert compare_values("foo", None) is True

    def test_whitespace_normalised(self):
        assert compare_values(" foo ", "foo") is False
        assert compare_values("\tfoo\n", "foo") is False

    def test_json_key_order_ignored(self):
        a = json.dumps({"a": 1, "b": 2})
        b = json.dumps({"b": 2, "a": 1})
        assert compare_values(a, b) is False

    def test_json_nested_key_order_ignored(self):
        a = json.dumps({"outer": {"x": 10, "y": 20}})
        b = json.dumps({"outer": {"y": 20, "x": 10}})
        assert compare_values(a, b) is False

    def test_order_insensitive_array_no_change(self):
        """Same elements, different order → NOT a change for order_insensitive fields."""
        a = json.dumps(["A", "B", "C"])
        b = json.dumps(["C", "A", "B"])
        assert compare_values(a, b, field_name="conditions") is False

    def test_order_sensitive_array_is_change(self):
        """Same elements, different order → IS a change for regular fields."""
        a = json.dumps(["A", "B", "C"])
        b = json.dumps(["C", "A", "B"])
        assert compare_values(a, b, field_name="title") is True

    def test_array_different_content(self):
        a = json.dumps(["A", "B"])
        b = json.dumps(["A", "C"])
        assert compare_values(a, b) is True

    def test_array_length_change(self):
        a = json.dumps(["A", "B"])
        b = json.dumps(["A", "B", "C"])
        assert compare_values(a, b) is True

    def test_empty_vs_nonempty_json(self):
        assert compare_values("[]", json.dumps(["X"])) is True
        assert compare_values(json.dumps(["X"]), "[]") is True

    def test_integer_values(self):
        assert compare_values(100, 100) is False
        assert compare_values(100, 200) is True

    def test_float_values(self):
        assert compare_values(1.5, 1.5) is False
        assert compare_values(1.5, 2.0) is True

    def test_order_insensitive_nested_arrays(self):
        """Nested lists with order_insensitive should sort recursively."""
        a = json.dumps([{"id": 2}, {"id": 1}])
        b = json.dumps([{"id": 1}, {"id": 2}])
        # order_insensitive=True, and the outer list is sorted by JSON repr
        assert compare_values(a, b, field_name="locations") is False

    def test_strip_vs_no_strip(self):
        """Only whitespace differences → not a change."""
        assert compare_values(True, True) is False
        assert compare_values(True, False) is True


# ═══════════════════════════════════════════════════════════════════════
#  compute_event_hash — pure function
# ═══════════════════════════════════════════════════════════════════════


class TestComputeEventHash:
    def test_deterministic(self):
        h1 = compute_event_hash(1, "NCT001", "enrollment", "200")
        h2 = compute_event_hash(1, "NCT001", "enrollment", "200")
        assert h1 == h2

    def test_different_source_id(self):
        h1 = compute_event_hash(1, "NCT001", "enrollment", "200")
        h2 = compute_event_hash(2, "NCT001", "enrollment", "200")
        assert h1 != h2

    def test_different_trial_id(self):
        h1 = compute_event_hash(1, "NCT001", "enrollment", "200")
        h2 = compute_event_hash(1, "NCT002", "enrollment", "200")
        assert h1 != h2

    def test_different_field(self):
        h1 = compute_event_hash(1, "NCT001", "enrollment", "200")
        h2 = compute_event_hash(1, "NCT001", "status_id", "200")
        assert h1 != h2

    def test_different_value(self):
        h1 = compute_event_hash(1, "NCT001", "enrollment", "200")
        h2 = compute_event_hash(1, "NCT001", "enrollment", "300")
        assert h1 != h2

    def test_none_value(self):
        h = compute_event_hash(1, "NCT001", "completion_date", None)
        assert isinstance(h, str) and len(h) == 64

    def test_json_value_normalised(self):
        """JSON values are normalised (key order) before hashing."""
        h1 = compute_event_hash(1, "NCT001", "conditions", json.dumps({"a": 1, "b": 2}))
        h2 = compute_event_hash(1, "NCT001", "conditions", json.dumps({"b": 2, "a": 1}))
        assert h1 == h2


# ═══════════════════════════════════════════════════════════════════════
#  detect_changes — integration (requires DB)
# ═══════════════════════════════════════════════════════════════════════


def _insert_record(conn, version: int, is_latest: int,
                   status_label: str, enrollment: int,
                   source_trial_id: str = "NCT001",
                   extra_fields: dict = None) -> int:
    """Helper: insert a row into registry_records and return record_id."""
    
    # Look up status FK
    cur = conn.execute(
        "SELECT status_type_id FROM status_types WHERE label = ?",
        (status_label,),
    )
    row = cur.fetchone()
    status_id = row["status_type_id"] if row else 1

    data = {
        "source_id": 1,
        "source_trial_id": source_trial_id,
        "title": f"Test v{version}",
        "status_id": status_id,
        "enrollment": enrollment,
        "conditions": json.dumps(["A", "B"]),
        "countries": json.dumps(["US"]),
        "data_hash": f"hash_v{version}_{enrollment}",
        "version_number": version,
        "is_latest": is_latest,
        "is_bootstrap": 0,
    }
    if extra_fields:
        data.update(extra_fields)

    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    cur = conn.execute(
        f"INSERT INTO registry_records ({cols}) VALUES ({placeholders})",
        list(data.values()),
    )
    conn.commit()
    return cur.lastrowid


class TestDetectChanges:
    def test_no_previous_version(self, test_db):
        """Version 1 with no previous_record_id → no changes."""
        from db.connection import get_connection
        conn = get_connection()
        rid = _insert_record(conn, version=1, is_latest=1,
                             status_label="Recruiting", enrollment=100)
        changes = detect_changes(record_id=rid)
        assert changes == []

    def test_same_data_no_changes(self, test_db):
        """Two versions with same tracked fields → no change events."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=100,
                                extra_fields={"superseded_by": old_id})
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)
        # Same status and enrollment → no changes
        assert changes == []

    def test_status_change_detected(self, test_db):
        """Status changed → change event with category 'status'."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Completed", enrollment=100,
                                extra_fields={"superseded_by": old_id})
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)

        # Only status changed (enrollment same)
        status_changes = [c for c in changes if c["field_name"] == "status_id"]
        assert len(status_changes) == 1
        assert status_changes[0]["category"] == "status"

    def test_enrollment_change_detected(self, test_db):
        """Enrollment changed → change event with category 'enrollment'."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=200,
                                extra_fields={"superseded_by": old_id})
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)

        enrollment_changes = [c for c in changes if c["field_name"] == "enrollment"]
        assert len(enrollment_changes) == 1
        assert enrollment_changes[0]["category"] == "enrollment"

    def test_order_insensitive_no_false_positive(self, test_db):
        """Reordered JSON array → NOT a change for order_insensitive fields."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100,
                                extra_fields={
                                    "conditions": json.dumps(["A", "B", "C"]),
                                })
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=100,
                                extra_fields={
                                    "conditions": json.dumps(["C", "A", "B"]),
                                    "superseded_by": old_id,
                                })
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)

        # conditions is order_insensitive, so no change should be detected for it
        condition_changes = [c for c in changes if c["field_name"] == "conditions"]
        assert len(condition_changes) == 0

    def test_multiple_changes(self, test_db):
        """Both status and enrollment changed → two events."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Completed", enrollment=200,
                                extra_fields={"superseded_by": old_id})
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)
        assert len(changes) >= 2
        categories = {c["field_name"] for c in changes}
        assert "status_id" in categories
        assert "enrollment" in categories
        assert "conditions" not in categories  # same in both


# ═══════════════════════════════════════════════════════════════════════
#  portal-live stub downgrade guard
# ═══════════════════════════════════════════════════════════════════════
# Regression for the 2026-09-29 ICTRP incident: a thin WHO portal row
# (title/status/date only) was versioned over a full snapshot record and
# the diff registered every field the grid never carried as "cleared" —
# 1144 phantom events across 183 trials.


class TestPortalStubDowngradeGuard:
    STUB_PAYLOAD = json.dumps({"who_ictrp_record": {
        "trial_id": "ChiCTR2400087372", "_portal_live": 1}})

    def test_stub_over_full_emits_no_events(self, test_db):
        """Downgrade suppresses all diff output — the grid-carried fields
        match anyway, everything else would be a phantom clear."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100,
                                source_trial_id="ChiCTR2400087372")
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=None,
                                source_trial_id="ChiCTR2400087372",
                                extra_fields={
                                    "conditions": None, "countries": None,
                                    "raw_payload": self.STUB_PAYLOAD,
                                    "superseded_by": old_id,
                                })
        assert detect_changes(record_id=new_id,
                              previous_record_id=old_id) == []

    def test_stub_downgrade_saves_no_trial_events(self, test_db):
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100,
                                source_trial_id="ChiCTR2400087372")
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=None,
                                source_trial_id="ChiCTR2400087372",
                                extra_fields={
                                    "sponsors": None,
                                    "primary_endpoint": None,
                                    "raw_payload": self.STUB_PAYLOAD,
                                    "superseded_by": old_id,
                                })
        saved = detect_and_save_events(
            record_id=new_id, previous_record_id=old_id,
            source_id=4, source_trial_id="ChiCTR2400087372",
        )
        assert saved == 0
        assert conn.execute(
            "SELECT count(*) FROM trial_events").fetchone()[0] == 0

    def test_full_cleared_field_still_detected(self, test_db):
        """Control: a REAL clear between two full rows still fires — the
        guard is scoped to portal stubs, not to clears generally."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100,
                                extra_fields={"countries": '["China"]'})
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=100,
                                extra_fields={"countries": None,
                                              "superseded_by": old_id})
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)
        assert any(c["field_name"] == "countries" for c in changes)

    def test_stub_to_stub_still_diffs(self, test_db):
        """thin-over-thin grid refresh keeps detecting status drift."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=None,
                                extra_fields={"raw_payload": self.STUB_PAYLOAD})
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Completed", enrollment=None,
                                extra_fields={
                                    "raw_payload": self.STUB_PAYLOAD,
                                    "superseded_by": old_id,
                                })
        changes = detect_changes(record_id=new_id, previous_record_id=old_id)
        assert any(c["field_name"] == "status_id" for c in changes)

    def test_malformed_payload_treated_as_full(self, test_db):
        """An unparseable payload never marks a row as a stub (and never
        raises) — the corrupted ChiCTR2400087372 v1 row stays 'full'."""
        from core.change_detection import _is_portal_live_stub

        assert _is_portal_live_stub(
            {"raw_payload": '{"who_ictrp_record": {"trial_i'}) is False
        assert _is_portal_live_stub({"raw_payload": None}) is False
        assert _is_portal_live_stub({"raw_payload": self.STUB_PAYLOAD}) is True


# ═══════════════════════════════════════════════════════════════════════
#  detect_and_save_events — trial_events persistence + dedup
# ═══════════════════════════════════════════════════════════════════════


class TestDetectAndSaveEvents:
    def test_events_persisted(self, test_db):
        """Changes are written to trial_events table."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Completed", enrollment=200,
                                extra_fields={"superseded_by": old_id})

        n = detect_and_save_events(
            record_id=new_id, previous_record_id=old_id,
            source_id=1, source_trial_id="NCT001",
        )
        assert n > 0

        cur = conn.execute("SELECT count(*) FROM trial_events")
        assert cur.fetchone()[0] == n

    def test_dedup_same_event(self, test_db):
        """Same event_hash → only one row persists."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Completed", enrollment=100,
                                extra_fields={"superseded_by": old_id})

        # Call twice — second should be deduped
        detect_and_save_events(
            record_id=new_id, previous_record_id=old_id,
            source_id=1, source_trial_id="NCT001",
        )
        detect_and_save_events(
            record_id=new_id, previous_record_id=old_id,
            source_id=1, source_trial_id="NCT001",
        )

        cur = conn.execute("SELECT count(*) FROM trial_events")
        total = cur.fetchone()[0]
        # Only unique events; duplicates suppressed
        assert total == 1

    def test_different_events_both_saved(self, test_db):
        """Different event_hashes → both saved (no dedup collision)."""
        from db.connection import get_connection
        conn = get_connection()

        # Trial A: status changed
        a_old = _insert_record(conn, version=1, is_latest=0,
                               status_label="Recruiting", enrollment=100,
                               source_trial_id="NCT001")
        a_new = _insert_record(conn, version=2, is_latest=1,
                               status_label="Completed", enrollment=100,
                               source_trial_id="NCT001",
                               extra_fields={"superseded_by": a_old})

        # Trial B: enrollment changed
        b_old = _insert_record(conn, version=1, is_latest=0,
                               status_label="Recruiting", enrollment=100,
                               source_trial_id="NCT002")
        b_new = _insert_record(conn, version=2, is_latest=1,
                               status_label="Recruiting", enrollment=300,
                               source_trial_id="NCT002",
                               extra_fields={"superseded_by": b_old})

        detect_and_save_events(
            record_id=a_new, previous_record_id=a_old,
            source_id=1, source_trial_id="NCT001",
        )
        detect_and_save_events(
            record_id=b_new, previous_record_id=b_old,
            source_id=1, source_trial_id="NCT002",
        )

        cur = conn.execute("SELECT count(*) FROM trial_events")
        assert cur.fetchone()[0] == 2  # one per trial

    def test_no_changes_no_events(self, test_db):
        """Identical versions → zero events saved."""
        from db.connection import get_connection
        conn = get_connection()
        old_id = _insert_record(conn, version=1, is_latest=0,
                                status_label="Recruiting", enrollment=100)
        new_id = _insert_record(conn, version=2, is_latest=1,
                                status_label="Recruiting", enrollment=100,
                                extra_fields={"superseded_by": old_id})

        n = detect_and_save_events(
            record_id=new_id, previous_record_id=old_id,
            source_id=1, source_trial_id="NCT001",
        )
        assert n == 0
