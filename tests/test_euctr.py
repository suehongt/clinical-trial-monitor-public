from __future__ import annotations

import json

import pytest

from collectors.euctr import EUCTRCollector, _parse_fields, _parse_summary
from core.ingestion import collector_for, run_registry_ingestion


SUMMARY = """EudraCT Number:          2014-001354-42
Sponsor Protocol Number: IAMI-2014
Sponsor Name:            Örebro University Hospital
Full Title:              Influenza vaccination after myocardial infarction
Start Date:              2014-12-16
Medical condition:       Myocardial infarction
Disease:                 Term: Myocardial infarction, Level: PT
Trial protocol:          GB(GB - no longer in EU/EEA) SE(Completed) DK(Completed)
Link:                    https://www.clinicaltrialsregister.eu/ctr-search/search?query=eudract_number:2014-001354-42
"""

DETAIL = """Summary
EudraCT Number: 2014-001354-42
Sponsor's Protocol Code Number: IAMI-2014
Trial Status: Completed
Date on which this record was first entered in the EudraCT database: 2014-11-20
A.1 Member State Concerned: Sweden - MPA
A.3 Full title of the trial: Influenza vaccination after myocardial infarction
B.1.1 Name of Sponsor: Örebro University Hospital
D.2.1.1.1 Trade name: Influvac
D.3.8 INN - Proposed INN: INFLUENZA VACCINE
E.1.1 Medical condition(s) being investigated: Myocardial infarction
E.1.2 Term: Acute myocardial infarction
E.3 Principal inclusion criteria: Adults after myocardial infarction
E.4 Principal exclusion criteria: Contraindication to vaccination
E.5.1 Primary end point(s): Composite cardiovascular outcome
E.5.2 Secondary end point(s): All-cause mortality
E.7.1 Human pharmacology (Phase I): No
E.7.2 Therapeutic exploratory (Phase II): No
E.7.3 Therapeutic confirmatory (Phase III): Yes
E.7.4 Therapeutic use (Phase IV): No
E.8.1 Controlled: Yes
E.8.1.1 Randomised: Yes
E.8.1.4 Double blind: Yes
F.4.2.2 In the whole clinical trial: 2571
P. Date of the global end of the trial: 2020-04-01
"""


def test_summary_parser_and_preferred_country():
    row = _parse_summary(SUMMARY)[0]
    assert row["source_trial_id"] == "2014-001354-42"
    assert row["protocols"] == [
        {"country_code": "GB", "status": "GB - no longer in EU/EEA"},
        {"country_code": "SE", "status": "Completed"},
        {"country_code": "DK", "status": "Completed"},
    ]


def test_euctr_normalises_full_protocol():
    summary = _parse_summary(SUMMARY)[0]
    raw = {**summary, "selected_country_code": "SE",
           "fields": _parse_fields(DETAIL), "_detail_text": DETAIL}
    record = EUCTRCollector().normalise(raw)
    assert record.source_trial_id == "2014-001354-42"
    assert record.status == "Completed"
    assert record.enrollment == 2571
    assert record.registration_date == "2014-11-20"
    assert record.start_date == "2014-12-16"
    assert record.completion_date == "2020-04-01"
    assert record.study_phase == "Phase 3"
    assert record.study_design == "Controlled; Randomized; Double blind"
    assert json.loads(record.conditions) == ["Myocardial infarction", "Acute myocardial infarction"]
    assert json.loads(record.interventions) == ["Influvac", "INFLUENZA VACCINE"]
    assert json.loads(record.countries) == ["United Kingdom", "Sweden", "Denmark"]
    assert json.loads(record.sponsors) == ["Örebro University Hospital"]
    assert json.loads(record.secondary_endpoints) == ["All-cause mortality"]
    payload = json.loads(record.raw_payload)
    assert payload["secondary_ids"] == ["IAMI-2014"]
    assert payload["_detail_text"] == DETAIL


def test_fetch_uses_summary_and_one_preferred_country(monkeypatch):
    class Response:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if url.endswith("/ctr-search/search"):
            return Response("Trials with a EudraCT protocol (1)")
        if url.endswith("/summary"):
            return Response(SUMMARY)
        assert url.endswith("/trial/2014-001354-42/SE")
        return Response(DETAIL)
    monkeypatch.setattr("collectors.euctr.requests.get", get)
    collector = EUCTRCollector()
    collector.cfg.max_records_per_run = 1
    collector.cfg.request_delay_sec = 0
    collector.cfg.extra["query_cond"] = "myocardial infarction OR STEMI"
    rows = collector.fetch_new_or_updated("2026-09-01 00:00:00")
    assert rows[0]["selected_country_code"] == "SE"
    assert rows[0]["fields"]["Trial Status"] == ["Completed"]
    # Bare words AND-score on EUCTR; quoted phrases make OR union.
    assert calls[0][1]["query"] == '"myocardial infarction" OR "STEMI"'
    assert calls[1][1]["mode"] == "current_page"


def test_crawl_all_excludes_manual_only_registries():
    from run_monitor import batch_crawl_sources
    batch = batch_crawl_sources(["clinicaltrials_gov", "ctis", "isrctn", "euctr"])
    assert "euctr" not in batch
    assert "ctis" in batch and "isrctn" in batch
    # Named explicit crawls bypass the batch filter entirely.
    assert batch_crawl_sources([]) == []


def test_euctr_is_seeded_primary_manual_only(test_db):
    from db.connection import get_connection
    row = get_connection().execute(
        "SELECT source_type,enabled FROM registry_sources WHERE short_name='EUCTR'"
    ).fetchone()
    assert dict(row) == {"source_type": "PRIMARY", "enabled": 0}
    assert isinstance(collector_for("euctr"), EUCTRCollector)
    with pytest.raises(ValueError, match="manual-only"):
        run_registry_ingestion("euctr", trigger="scheduled")
