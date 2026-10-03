"""Durability of new-record upserts.

Regression test: the 'new' branch of BaseCollector._upsert_record used to
insert without committing — records survived only if some later incidental
commit happened on the same connection.  A direct upsert (or a crash
mid-crawl) silently lost the data once the connection closed.
"""
from __future__ import annotations


def test_new_record_persists_after_connection_close(test_db):
    from db.schema import create_schema
    create_schema()

    from collectors.who_ictrp import WHOICTRPCollector
    collector = WHOICTRPCollector()
    nr = collector.normalise({
        "reg_name": "ClinicalTrials.gov",
        "trial_id": "NCT88888888",
        "public_title": "Durability Probe",
        "recruitment_status": "Recruiting",
    })
    result = collector._upsert_record(nr)
    assert result["action"] == "new"

    # Drop the thread-local connection — uncommitted work would be lost here.
    from db.connection import close_connection, get_connection
    close_connection()
    conn = get_connection()

    row = conn.execute(
        "SELECT count(*) FROM registry_records WHERE source_trial_id = 'NCT88888888'"
    ).fetchone()[0]
    assert row == 1, "new record was not durable across connection close"
