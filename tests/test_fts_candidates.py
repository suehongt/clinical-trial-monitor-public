"""FTS-based title candidate selection for entity resolution.

Regression tests for the fix of the arbitrary `LIMIT 200` candidate window:
a similar title linked far beyond the first 200 rows must still be found.
"""
from __future__ import annotations

import uuid as _uuid

import pytest


def _linked_trial(conn, source_id, trial_id: str, title: str) -> int:
    """Insert a record linked to its own master trial.  Returns record_id."""
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, version_number, is_latest)
           VALUES (?, ?, ?, 1, 1)""",
        (source_id, trial_id, title),
    )
    record_id = cur.lastrowid
    master_id = str(_uuid.uuid4())
    conn.execute(
        "INSERT INTO master_trials (master_trial_id, preferred_title) VALUES (?, ?)",
        (master_id, title),
    )
    conn.execute(
        """INSERT INTO record_master_map
           (record_id, master_trial_id, match_method, match_confidence, match_status)
           VALUES (?, ?, 'first_record', 1.0, 'AUTO_CONFIRMED')""",
        (record_id, master_id),
    )
    return record_id


@pytest.fixture
def populated_db(test_db):
    from db.connection import get_connection
    from db.schema import create_schema

    create_schema()
    conn = get_connection()
    source_id = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name='NCT'"
    ).fetchone()[0]

    # 205 filler trials (each linked) push the target beyond the old
    # arbitrary candidate window of 200.
    for i in range(205):
        _linked_trial(conn, source_id, f"NCT{i:08d}",
                      f"Filler Study {i} On Diabetes Management")
    target_id = _linked_trial(
        conn, source_id, "NCT00000206",
        "Novel Stent Implantation Strategy for Acute Myocardial Infarction",
    )
    # Unlinked record with a near-identical title to the target
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, version_number, is_latest)
           VALUES (?, 'NCT00000207',
                   'A Novel Stent Implantation Strategy for Acute '
                   || 'Myocardial Infarction Patients', 1, 1)""",
        (source_id,),
    )
    unlinked_id = cur.lastrowid
    conn.commit()
    return conn, target_id, unlinked_id


class TestTitleCandidates:
    def test_similar_title_beyond_first_200_is_found(self, populated_db):
        from core.entity_resolution import find_master_matches
        from core.identifier_extraction import extract_and_save_identifiers

        conn, target_id, unlinked_id = populated_db
        matches = find_master_matches(
            unlinked_id, extract_and_save_identifiers(unlinked_id),
        )
        title_matches = [m for m in matches if m["method"] == "title_similarity"]
        assert title_matches, "expected a title_similarity candidate"
        assert title_matches[0]["master_trial_id"] == conn.execute(
            "SELECT master_trial_id FROM record_master_map WHERE record_id = ?",
            (target_id,),
        ).fetchone()[0]
        assert title_matches[0]["match_status"] == "POSSIBLE"

    def test_resolve_queues_instead_of_creating_new_master(self, populated_db):
        from core.entity_resolution import resolve_record

        conn, target_id, unlinked_id = populated_db
        result = resolve_record(unlinked_id)
        assert result["action"] == "queued"
        assert result["match_status"] == "POSSIBLE"

    def test_dissimilar_titles_do_not_match(self, populated_db):
        from core.entity_resolution import _title_candidates
        from db.connection import get_connection

        conn = get_connection()
        source_id = conn.execute(
            "SELECT source_id FROM registry_sources WHERE short_name='NCT'"
        ).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, version_number, is_latest)
               VALUES (?, 'NCT00000208',
                       'Phase 2 Trial of Orlistat in Obesity', 1, 1)""",
            (source_id,),
        )
        unrelated_id = cur.lastrowid
        conn.commit()

        candidates = _title_candidates(
            "Phase 2 Trial of Orlistat in Obesity", unrelated_id,
        )
        target_titles = [
            c["title"] for c in candidates
            if "Myocardial" in (c["title"] or "")
        ]
        assert not target_titles
