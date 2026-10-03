from __future__ import annotations

import json
import threading

from collectors.ctis import CTISCollector
from core.ingestion import collector_for


def _raw():
    return {
        "_search": {
            "ctNumber": "2026-525681-22-00",
            "ctTitle": "Scientific title",
            "ctStatus": "2",
            "trialPhase": "Phase III",
            "totalNumberEnrolled": "2126",
            "lastUpdated": "25/09/2026",
            "sponsor": "Example Sponsor",
        },
        "_detail": {
            "ctNumber": "2026-525681-22-00",
            "ctStatus": "Authorised",
            "publishDate": "2026-09-26T03:42:53.933825371",
            "authorizedApplication": {
                "authorizedPartI": {
                    "trialDetails": {
                        "clinicalTrialIdentifiers": {
                            "publicTitle": "Public title",
                            "fullTitle": "Scientific title",
                        },
                        "trialInformation": {
                            "medicalCondition": {
                                "partIMedicalConditions": [{"medicalCondition": "Myocardial infarction"}],
                                "meddraConditionTerms": [{"termName": "Acute coronary syndrome"}],
                            },
                            "trialDuration": {
                                "estimatedRecruitmentStartDate": "01/10/2026",
                                "estimatedEndDate": "2028-12-31",
                            },
                            "eligibilityCriteria": {
                                "principalInclusionCriteria": [{"principalInclusionCriteria": "Adult"}],
                                "principalExclusionCriteria": [{"principalExclusionCriteria": "Pregnancy"}],
                            },
                            "endPoint": {
                                "primaryEndPoints": [{"endPoint": "MACE"}],
                                "secondaryEndPoints": [{"endPoint": "Mortality"}],
                            },
                        },
                    },
                    "products": [{"productName": "Aspirin"}],
                    "sponsors": [{"organisation": {"name": "Example Sponsor"}}],
                },
                "authorizedPartsII": [{
                    "mscInfo": {"countryName": "Germany"},
                    "trialSites": [{"organisationAddressInfo": {
                        "organisation": {"name": "University Hospital"},
                        "address": {"city": "Berlin", "countryName": "Germany"},
                    }}],
                }],
            },
        },
    }


def test_ctis_normalises_public_detail():
    record = CTISCollector().normalise(_raw())
    assert record.source_trial_id == "2026-525681-22-00"
    assert record.title == "Public title"
    assert record.status == "Registered"
    assert record.enrollment == 2126
    assert record.start_date == "2026-10-01"
    assert json.loads(record.conditions) == ["Myocardial infarction", "Acute coronary syndrome"]
    assert json.loads(record.interventions) == ["Aspirin"]
    assert json.loads(record.countries) == ["Germany"]
    assert json.loads(record.locations)[0]["city"] == "Berlin"
    assert json.loads(record.secondary_endpoints) == ["Mortality"]
    assert json.loads(record.raw_payload)["_detail"]["ctNumber"] == "2026-525681-22-00"


def test_ctis_fetches_search_then_detail(monkeypatch):
    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    calls = []
    monkeypatch.setattr("collectors.ctis.requests.post", lambda url, **kw: (
        calls.append(("post", url, kw["json"])) or
        Response({"data": [{"ctNumber": "2026-525681-22-00", "lastUpdated": "25/09/2026"}],
                  "pagination": {"totalPages": 1}})))
    monkeypatch.setattr("collectors.ctis.requests.get", lambda url, **kw: (
        calls.append(("get", url, None)) or Response({"ctNumber": "2026-525681-22-00"})))
    collector = CTISCollector()
    collector.cfg.max_records_per_run = 1
    collector.cfg.request_delay_sec = 0
    collector.cfg.extra["query_cond"] = "myocardial infarction OR acute coronary syndrome"
    rows = collector.fetch_new_or_updated()
    assert rows[0]["_detail"]["ctNumber"] == "2026-525681-22-00"
    # CTIS containAny treats the raw "A OR B" string as one literal phrase
    # (live: 0 hits); terms must arrive comma-joined.
    assert calls[0][2]["searchCriteria"] == {"containAny": "myocardial infarction,acute coronary syndrome"}
    assert calls[0][2]["pagination"] == {"page": 1, "size": 1}
    assert calls[1][1].endswith("/retrieve/2026-525681-22-00")


def test_ctis_boolean_next_page_advances(monkeypatch):
    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    pages = []
    def post(_url, **kwargs):
        page = kwargs["json"]["pagination"]["page"]
        pages.append(page)
        return Response({
            "data": [{"ctNumber": f"2026-50000{page}-00-00"}],
            "pagination": {"totalPages": 2, "nextPage": page < 2},
        })
    monkeypatch.setattr("collectors.ctis.requests.post", post)
    monkeypatch.setattr("collectors.ctis.requests.get", lambda url, **kw: Response({"ctNumber": url.rsplit("/", 1)[-1]}))
    collector = CTISCollector()
    collector.cfg.max_records_per_run = 0
    collector.cfg.request_delay_sec = 0
    assert len(collector.fetch_new_or_updated()) == 2
    assert pages == [1, 2]


def test_ctis_overlaps_details_but_preserves_search_order(monkeypatch):
    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    monkeypatch.setattr("collectors.ctis.requests.post", lambda *_a, **_kw: Response({
        "data": [{"ctNumber": "CT-1"}, {"ctNumber": "CT-2"}],
        "pagination": {"totalPages": 1},
    }))
    lock = threading.Lock()
    both_started = threading.Event()
    active = 0
    peak = 0

    def get(url, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
        assert both_started.wait(1), "detail requests did not overlap"
        with lock:
            active -= 1
        return Response({"ctNumber": url.rsplit("/", 1)[-1]})

    monkeypatch.setattr("collectors.ctis.requests.get", get)
    collector = CTISCollector()
    collector.cfg.max_records_per_run = 2
    collector.cfg.request_delay_sec = 0
    collector.cfg.extra["detail_workers"] = 2
    rows = collector.fetch_new_or_updated()

    assert peak == 2
    assert [row["_detail"]["ctNumber"] for row in rows] == ["CT-1", "CT-2"]


def test_new_source_rows_are_primary_and_dispatchable(test_db):
    from db.connection import get_connection
    rows = {row["short_name"]: dict(row) for row in get_connection().execute(
        "SELECT short_name, source_type, enabled FROM registry_sources WHERE short_name IN ('CTIS','ISRCTN')"
    )}
    assert rows == {
        "CTIS": {"short_name": "CTIS", "source_type": "PRIMARY", "enabled": 0},
        "ISRCTN": {"short_name": "ISRCTN", "source_type": "PRIMARY", "enabled": 0},
    }
    assert isinstance(collector_for("ctis"), CTISCollector)
