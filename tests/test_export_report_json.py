"""Exporter contract tests: detail keys must be globally unique.

Regression for the review finding: details were keyed by source_trial_id
alone, but ICTRP mirrors reuse the source registry's ID — colliding with
the primary records and silently swapping detail texts between cards.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def two_same_id_records(test_db):
    """Two records sharing source_trial_id 'NCT00000042' from NCT + ICTRP."""
    from db.schema import create_schema
    create_schema()
    from db.connection import get_connection
    conn = get_connection()
    for short, sid in (("NCT", "NCT00000042"), ("ICTRP", "NCT00000042")):
        conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, version_number, is_latest)
               VALUES ((SELECT source_id FROM registry_sources WHERE short_name=?),
                       ?, 'Heart Failure Screening Keyword', 1, 1)""",
            (short, sid),
        )
    conn.commit()
    yield conn


def test_detail_keys_are_source_scoped(two_same_id_records, monkeypatch):
    # query_trials reads via ct_report.query.DB_PATH (independent of
    # db.connection) — point it at the fixture DB too.
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)
    from scripts.export_report_json import build_profile_payload
    payload, details = build_profile_payload("hf")
    assert payload["total"] == 2
    assert set(details) == {"NCT:NCT00000042", "ICTRP:NCT00000042"}


def test_light_payload_has_no_heavy_detail_fields(two_same_id_records, monkeypatch):
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)
    from scripts.export_report_json import build_profile_payload
    payload, _ = build_profile_payload("hf")
    t = payload["trials"][0]
    for heavy in ("summary", "inclusion", "exclusion", "investigator",
                  "endpointTable", "_details"):
        assert heavy not in t, f"heavy field '{heavy}' must live in details"


def test_light_payload_cleans_registry_html_from_primary_endpoint():
    """ICTRP outcome markup becomes readable text, never raw/active HTML."""
    from scripts.export_report_json import build_light_trial

    trial = build_light_trial({
        "source_trial_id": "ICTRP-1",
        "short_name": "ICTRP",
        "title": "Outcome formatting regression",
        "primary_endpoint": (
            "Confidence interval<br />90% limit.<br/ ><br>"
            "<p>Freeze and thaw acceptance criteria</p>"
            "<script>alert('unsafe')</script>"
        ),
    })

    assert trial["primaryEndpoint"] == (
        "Confidence interval\n90% limit.\n\n"
        "Freeze and thaw acceptance criteriaalert('unsafe')"
    )
    assert "<" not in trial["primaryEndpoint"]
