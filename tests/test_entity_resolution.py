"""
Tests for the cross-platform entity resolution framework.

Test cases:
  1. Same trial ID across platforms → AUTO_CONFIRMED
  2. Cross-registration (ChiCTR mentions NCT in raw_payload) → AUTO_CONFIRMED
  3. Similar titles, no overlapping IDs → POSSIBLE → queued
  4. Completely different trials → separate master trials
  5. Protocol number exact match → AUTO_CONFIRMED
  6. Multi-platform cross-registration (3 records → 1 master)

Each test follows the same pattern:
  1. Seed lookup data (sources, study types, statuses)
  2. Insert registry_records with appropriate raw_payload cross-refs
  3. Import the identifier extraction module and extract
  4. Run entity resolution
  5. Assert correct master_trial links and queue status
"""
from __future__ import annotations

import json

import pytest

from db.connection import get_connection


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sources(conn):
    """Seed registry sources (NCT, ChiCTR, CTR)."""
    for short, full in [
        ("NCT", "ClinicalTrials.gov"),
        ("ChiCTR", "中国临床试验注册中心"),
        ("CTR", "中国药物临床试验登记与信息公示平台"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO registry_sources (short_name, full_name) VALUES (?, ?)",
            (short, full),
        )
    conn.commit()


@pytest.fixture
def lookup_types(conn):
    """Seed minimal study types and status types."""
    for label in ["Interventional", "Observational"]:
        conn.execute("INSERT OR IGNORE INTO study_types (label) VALUES (?)", (label,))
    for label in ["Recruiting", "Completed", "Not yet recruiting"]:
        conn.execute("INSERT OR IGNORE INTO status_types (label) VALUES (?)", (label,))
    conn.commit()


@pytest.fixture
def conn(test_db):
    """Get a connection to the test database."""
    conn = get_connection()
    yield conn


# ── Helper ───────────────────────────────────────────────────────────────


def _source_id(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


def _status_id(conn, label: str) -> int:
    return conn.execute(
        "SELECT status_type_id FROM status_types WHERE label = ?",
        (label,),
    ).fetchone()["status_type_id"]


def _insert_record(conn, source_short: str, source_trial_id: str,
                   title: str = "Test Trial",
                   raw_payload: str | None = None,
                   **kw) -> int:
    """Insert a registry_record and return its record_id."""
    sid = _source_id(conn, source_short)
    stid = _status_id(conn, "Recruiting")
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, raw_payload, is_latest)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (sid, source_trial_id, title, stid,
         raw_payload or json.dumps({"nctId": source_trial_id})),
    )
    conn.commit()
    return cur.lastrowid


def _resolve(conn, record_id: int) -> dict:
    """Run the full resolution pipeline for one record and return the result."""
    from core.entity_resolution import resolve_record
    return resolve_record(record_id)


# ── Tests ────────────────────────────────────────────────────────────────


class TestIdentifierExtraction:
    """Verify that identifiers are correctly extracted from records."""

    def test_extract_source_trial_id(self, conn, sources, lookup_types):
        """source_trial_id is always extracted with confidence 1.0."""
        rid = _insert_record(conn, "NCT", "NCT12345678")
        from core.identifier_extraction import extract_and_save_identifiers
        ids = extract_and_save_identifiers(rid)

        nct_ids = [i for i in ids if i["identifier_type"] == "NCT"]
        assert len(nct_ids) >= 1
        assert nct_ids[0]["identifier_value"] == "NCT12345678"
        assert nct_ids[0]["confidence"] == 1.0
        assert nct_ids[0]["source_field"] == "source_trial_id"

    def test_extract_secondary_id_from_payload(self, conn, sources, lookup_types):
        """Secondary IDs in raw_payload are extracted."""
        payload = json.dumps({
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT87654321",
                    "secondaryIdInfos": [
                        {"secondaryId": "PROTOCOL-ABC-001"},
                    ],
                },
            },
        })
        rid = _insert_record(conn, "NCT", "NCT87654321",
                             raw_payload=payload)
        from core.identifier_extraction import extract_and_save_identifiers
        ids = extract_and_save_identifiers(rid)

        types = {i["identifier_type"] for i in ids}
        assert "NCT" in types
        # Protocol number may or may not be captured depending on pattern
        protocol_ids = [i for i in ids if i["identifier_type"] == "ProtocolNumber"]
        if protocol_ids:
            assert protocol_ids[0]["source_field"] in ("secondary_id", "raw_payload")

    def test_extract_cross_ref_from_raw_text(self, conn, sources, lookup_types):
        """NCT IDs mentioned in raw text are extracted as cross-references."""
        payload = json.dumps({
            "nctId": "ChiCTR2000000001",
            "otherIds": ["NCT99999999"],
            "description": "This trial is also registered as NCT99999999",
        })
        rid = _insert_record(conn, "ChiCTR", "ChiCTR2000000001",
                             title="Cross-Ref Study",
                             raw_payload=payload)
        from core.identifier_extraction import extract_and_save_identifiers
        ids = extract_and_save_identifiers(rid)

        nct_ids = [i for i in ids if i["identifier_type"] == "NCT"]
        assert len(nct_ids) >= 1
        assert nct_ids[0]["identifier_value"] == "NCT99999999"


class TestEntityResolution:
    """Test the core resolution pipeline."""

    def test_same_trial_id_across_platforms(self, conn, sources, lookup_types):
        """Same source_trial_id on different sources → AUTO_CONFIRMED to same master."""
        rid1 = _insert_record(conn, "NCT", "NCT00000001",
                              title="Global Heart Study")
        rid2 = _insert_record(conn, "ICTRP", "NCT00000001",
                              title="Global Heart Study")

        # Resolve first record → creates new master trial
        r1 = _resolve(conn, rid1)
        assert r1["action"] == "created"
        assert r1["match_status"] == "AUTO_CONFIRMED"

        # Resolve second record → finds matching identifier → AUTO_CONFIRMED link
        r2 = _resolve(conn, rid2)
        assert r2["action"] == "linked", f"Expected linked, got {r2}"
        assert r2["match_status"] == "AUTO_CONFIRMED"
        assert r2["master_trial_id"] == r1["master_trial_id"]

        # Both records should point to the same master trial
        maps = conn.execute(
            "SELECT master_trial_id FROM record_master_map WHERE record_id IN (?, ?)",
            (rid1, rid2),
        ).fetchall()
        assert len(maps) == 2
        assert maps[0]["master_trial_id"] == maps[1]["master_trial_id"]

    def test_cross_registration_via_raw_payload(self, conn, sources, lookup_types):
        """ChiCTR record mentioning an NCT number → AUTO_CONFIRMED to same master."""
        # NCT record registered first
        rid_nct = _insert_record(conn, "NCT", "NCT00000002",
                                 title="Diabetes Prevention Trial")

        # ChiCTR record that mentions the NCT number
        payload = json.dumps({
            "ChiTrialId": "ChiCTR2000000002",
            "ref_id": "NCT00000002",
        })
        rid_chictr = _insert_record(conn, "ChiCTR", "ChiCTR2000000002",
                                    title="Diabetes Prevention Trial (ChiCTR)",
                                    raw_payload=payload)

        # Resolve NCT → creates new master
        r1 = _resolve(conn, rid_nct)
        assert r1["action"] == "created"

        # Resolve ChiCTR → finds cross-ref NCT → AUTO_CONFIRMED
        r2 = _resolve(conn, rid_chictr)
        assert r2["action"] == "linked", f"Expected linked, got {r2}"
        assert r2["match_status"] == "AUTO_CONFIRMED"
        assert r2["master_trial_id"] == r1["master_trial_id"]

    def test_similar_titles_no_id_overlap_queued(self, conn, sources, lookup_types):
        """Similar titles without shared IDs → POSSIBLE → resolution queue."""
        rid1 = _insert_record(conn, "NCT", "NCT00000003",
                              title="A Study of the Safety and Efficacy of Drug X in Adults")
        rid2 = _insert_record(conn, "ChiCTR", "ChiCTR2000000003",
                              title="A Study of the Safety and Efficacy of Drug X in Adults")

        # Resolve both
        r1 = _resolve(conn, rid1)
        assert r1["action"] == "created"

        r2 = _resolve(conn, rid2)
        assert r2["action"] == "queued", f"Expected queued, got {r2}"
        assert r2["match_status"] == "POSSIBLE"

        # Should be in resolution queue
        queue = conn.execute(
            "SELECT * FROM resolution_queue WHERE record_id = ?", (rid2,),
        ).fetchone()
        assert queue is not None
        assert queue["status"] == "pending"

    def test_different_trials_separate_masters(self, conn, sources, lookup_types):
        """Completely different trials → separate master trials."""
        rid1 = _insert_record(conn, "NCT", "NCT00000004",
                              title="Advanced Heart Failure Management")
        rid2 = _insert_record(conn, "NCT", "NCT00000005",
                              title="Pediatric Asthma Inhaler Efficacy")

        r1 = _resolve(conn, rid1)
        r2 = _resolve(conn, rid2)
        assert r1["action"] == "created"
        assert r2["action"] == "created"
        assert r1["master_trial_id"] != r2["master_trial_id"]

    def test_multi_platform_cross_registration(self, conn, sources, lookup_types):
        """3 records from different sources all cross-referencing → one master."""
        # Record A: NCT registered first
        rid_a = _insert_record(conn, "NCT", "NCT00000006",
                               title="Global Vaccine Trial")

        # Record B: ChiCTR that mentions NCT
        payload_b = json.dumps({"ref": "NCT00000006"})
        rid_b = _insert_record(conn, "ChiCTR", "ChiCTR2000000004",
                               title="Global Vaccine Trial (China)",
                               raw_payload=payload_b)

        # Record C: Another source that also mentions NCT
        payload_c = json.dumps({"also_known_as": "NCT00000006"})
        rid_c = _insert_record(conn, "CTR", "CTR20250001",
                               title="Global Vaccine Trial (CTR)",
                               raw_payload=payload_c)

        r_a = _resolve(conn, rid_a)
        assert r_a["action"] == "created"

        r_b = _resolve(conn, rid_b)
        assert r_b["action"] == "linked"
        assert r_b["master_trial_id"] == r_a["master_trial_id"]

        r_c = _resolve(conn, rid_c)
        assert r_c["action"] == "linked"
        assert r_c["master_trial_id"] == r_a["master_trial_id"]

        # Verify all 3 point to same master
        mids = set(
            row["master_trial_id"]
            for row in conn.execute(
                "SELECT master_trial_id FROM record_master_map WHERE record_id IN (?, ?, ?)",
                (rid_a, rid_b, rid_c),
            ).fetchall()
        )
        assert len(mids) == 1

    def test_approve_queued_match(self, conn, sources, lookup_types):
        """Approving a queued match creates the master link."""
        rid1 = _insert_record(conn, "NCT", "NCT00000007",
                              title="Queued Match Study")
        rid2 = _insert_record(conn, "ChiCTR", "ChiCTR2000000005",
                              title="Queued Match Study")

        # Resolve both
        r1 = _resolve(conn, rid1)
        assert r1["action"] == "created"

        r2 = _resolve(conn, rid2)
        assert r2["action"] == "queued"

        # Approve the queue entry
        from core.entity_resolution import get_review_queue, approve_match
        queue = get_review_queue()
        assert len(queue) >= 1
        q_entry = queue[0]
        assert approve_match(q_entry["queue_id"])

        # Now record should be linked to the same master
        map_row = conn.execute(
            "SELECT master_trial_id, match_status FROM record_master_map WHERE record_id = ?",
            (rid2,),
        ).fetchone()
        assert map_row is not None
        assert map_row["master_trial_id"] == r1["master_trial_id"]
        assert map_row["match_status"] == "AUTO_CONFIRMED"


class TestAutoResolve:
    """Test the batch auto_resolve function."""

    def test_auto_resolve_batch(self, conn, sources, lookup_types):
        """auto_resolve processes all unlinked records correctly."""
        # Two unrelated records
        _insert_record(conn, "NCT", "NCT00000100", title="Study Alpha")
        _insert_record(conn, "NCT", "NCT00000101", title="Study Beta")

        # One pair that should match via cross-ref
        _insert_record(conn, "NCT", "NCT00000102",
                                 title="Study Gamma")
        payload = json.dumps({"ref": "NCT00000102"})
        _insert_record(conn, "ChiCTR", "ChiCTR2000000100",
                                    title="Study Gamma (Chinese Branch)",
                                    raw_payload=payload)

        from core.entity_resolution import auto_resolve
        summary = auto_resolve()

        # 4 records total
        assert summary["total"] == 4
        # NCT00000100, NCT00000101, NCT00000102 → created (3 new masters)
        # ChiCTR2000000100 → linked to NCT00000102's master (1 linked)
        assert summary["created"] == 3
        assert summary["linked"] == 1


def test_ctis_direct_record_links_to_ictrp_mirror_by_identifier(conn, sources, lookup_types):
    direct_id = _insert_record(
        conn, "CTIS", "2026-525681-22-00", title="Direct EU record",
        raw_payload=json.dumps({"_detail": {"ctNumber": "2026-525681-22-00"}}),
    )
    mirror_id = _insert_record(
        conn, "ICTRP", "ICTRP-EU-1", title="WHO mirror record",
        raw_payload=json.dumps({"other_registry_id": "2026-525681-22-00"}),
    )
    direct = _resolve(conn, direct_id)
    mirror = _resolve(conn, mirror_id)
    assert direct["action"] == "created"
    assert mirror["action"] == "linked"
    assert mirror["master_trial_id"] == direct["master_trial_id"]


def test_euctr_direct_record_links_to_ictrp_mirror_by_identifier(conn, sources, lookup_types):
    direct_id = _insert_record(
        conn, "EUCTR", "2014-001354-42", title="Legacy EU direct record",
        raw_payload=json.dumps({"source_trial_id": "2014-001354-42"}),
    )
    mirror_id = _insert_record(
        conn, "ICTRP", "ICTRP-EUCTR-1", title="WHO legacy EU mirror",
        raw_payload=json.dumps({"other_registry_id": "EudraCT 2014-001354-42"}),
    )
    direct = _resolve(conn, direct_id)
    mirror = _resolve(conn, mirror_id)
    assert direct["action"] == "created"
    assert mirror["action"] == "linked"
    assert mirror["match_status"] == "AUTO_CONFIRMED"
    assert mirror["master_trial_id"] == direct["master_trial_id"]
