"""server.app contract tests (web platform, Phase 7 P1).

The API must emit the exact JSON contract the web viewer's guards validate
(web/src/guards.ts): Trial / TrialDetailData / MirrorGroup / ChangeEvent
shapes plus generated_at.  These tests pin that contract from the Python
side against a temp database seeded with a small cross-source fixture.

Override pattern mirrors tests/test_export_report_json.py: the ``test_db``
fixture repoints ``CONFIG.db.path``; query_trials reads via
``ct_report.query.DB_PATH``, which is monkeypatched onto the same path.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest


# ── seed helpers ──────────────────────────────────────────────────────────


def _insert_record(
    conn,
    short_name: str,
    trial_id: str,
    title: str,
    *,
    sci: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    enrollment: int | None = None,
    conditions: str | None = None,
    sponsors: str | None = None,
    countries: str | None = None,
    locations: str | None = None,
    interventions: str | None = None,
    primary: str | None = None,
    secondary: str | None = None,
    eligibility: str | None = None,
    source_url: str | None = None,
    raw_payload: str | None = None,
    crawled: str = "2026-09-01 00:00:00",
    first_crawled: str = "2026-08-15 08:00:00",
) -> int:
    status_id = None
    if status is not None:
        status_id = conn.execute(
            "SELECT status_type_id FROM status_types WHERE label=?",
            (status,),
        ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, scientific_title, enrollment,
            status_id, study_phase, conditions, sponsors, countries, locations,
            interventions, primary_endpoint, secondary_endpoints,
            eligibility_criteria, source_url, raw_payload, last_crawled_at,
            first_crawled_at, version_number, is_latest)
           VALUES ((SELECT source_id FROM registry_sources WHERE short_name=?),
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
        (
            short_name, trial_id, title, sci, enrollment,
            status_id, phase, conditions, sponsors, countries, locations,
            interventions, primary, secondary, eligibility, source_url,
            raw_payload, crawled, first_crawled,
        ),
    )
    return cur.lastrowid


def _insert_event(conn, record_id: int, field: str, old: str | None,
                  new: str | None, category: str | None = None,
                  detected: str = "2026-09-12 08:30:00") -> None:
    conn.execute(
        """INSERT INTO trial_events
           (record_id, field_name, old_value, new_value, change_category,
            event_hash, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (record_id, field, old, new, category,
         hashlib.sha256(f"{record_id}:{field}:{old}:{new}:{detected}".encode()).hexdigest(),
         detected),
    )


def _link_master(conn, record_id: int, master_id: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO master_trials
           (master_trial_id, preferred_title) VALUES (?, ?)""",
        (master_id, "master"),
    )
    conn.execute(
        """INSERT INTO record_master_map
           (record_id, master_trial_id, match_method, match_status)
           VALUES (?, ?, 'identifier', 'AUTO_CONFIRMED')""",
        (record_id, master_id),
    )


@pytest.fixture
def seeded_client(test_db, monkeypatch):
    """Temp schema + a small NCT/ChiCTR fixture + a TestClient on server.app."""
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    from db.connection import get_connection
    conn = get_connection()

    nct_hf_payload = json.dumps({
        "protocolSection": {
            "descriptionModule": {"briefSummary": "A heart failure device study."},
            "contactsLocationsModule": {
                "overallOfficials": [
                    {"role": "PRINCIPAL_INVESTIGATOR", "name": "Dr Li",
                     "affiliation": "Beijing Heart Hospital"},
                ],
            },
        },
    }, ensure_ascii=False)

    rid_nct = _insert_record(
        conn, "NCT", "NCT00000001", "Heart failure device trial in adults",
        sci="Investigational device in chronic heart failure",
        status="Recruiting", phase="Phase 2", enrollment=120,
        conditions='["Heart Failure"]', sponsors='["Acme Medical"]',
        countries='["China", "United States"]',
        locations='["Beijing, China"]', interventions='["Device: LVAD"]',
        primary="Mortality at 12 months", secondary='["Quality of life"]',
        eligibility="Inclusion Criteria: adults with chronic heart failure\n"
                    "Exclusion Criteria: pregnancy",
        source_url="https://clinicaltrials.gov/study/NCT00000001",
        raw_payload=nct_hf_payload,
    )
    # status events carry integer FKs (like change detection writes them) —
    # the API must translate them to labels in detail history AND global stream
    old_sid = conn.execute(
        "SELECT status_type_id FROM status_types WHERE label='Not yet recruiting'"
    ).fetchone()[0]
    new_sid = conn.execute(
        "SELECT status_type_id FROM status_types WHERE label='Recruiting'"
    ).fetchone()[0]
    _insert_event(conn, rid_nct, "status_id", str(old_sid), str(new_sid),
                  "status_change")
    _insert_event(conn, rid_nct, "enrollment", "100", "120", "size_change")

    # a superseded version row carrying earlier history — the per-trial
    # timeline must span the whole version chain, not just the latest version
    # (version numbers must be distinct per (source, trial_id); the chain
    # link is superseded_by, which the timeline query follows implicitly)
    old_ver = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, version_number, is_latest,
            superseded_by)
           VALUES ((SELECT source_id FROM registry_sources WHERE short_name='NCT'),
                   'NCT00000001', 'Heart failure device trial (v1)', 2, 0, ?)""",
        (rid_nct,),
    ).lastrowid
    _insert_event(conn, old_ver, "enrollment", "80", "100", "size_change",
                  detected="2026-09-10 08:00:00")

    _insert_record(
        conn, "NCT", "NCT00000002", "Myocardial infarction registry study",
        status="Completed", phase="N/A", enrollment=50,
        conditions='["Myocardial Infarction"]', sponsors='["Registry Org"]',
        countries='["United States"]',
    )

    rid_chi = _insert_record(
        conn, "ChiCTR", "ChiCTR2100012345", "心力衰竭中医药干预试验",
        status="Recruiting", phase="Phase 3", enrollment=200,
        conditions='["心力衰竭"]', sponsors='["某中医院"]', countries='["中国"]',
        eligibility="入选标准：慢性心力衰竭患者\n排除标准：严重肝肾功能不全",
        source_url="https://www.chictr.org.cn/showproj.html?proj=ChiCTR2100012345",
    )

    master = str(uuid.uuid4())
    _link_master(conn, rid_nct, master)
    _link_master(conn, rid_chi, master)

    conn.execute(
        """INSERT INTO sync_status (source_id, last_successful_sync)
           VALUES ((SELECT source_id FROM registry_sources WHERE short_name='NCT'),
                   '2026-09-12 08:00:00')"""
    )
    conn.commit()

    from fastapi.testclient import TestClient
    from server.app import create_app

    yield TestClient(create_app())


# ── shared shape assertions (Python mirrors of web/src/guards.ts) ─────────


def _assert_trial_shape(t) -> None:
    assert isinstance(t["id"], str) and t["id"]
    assert isinstance(t["source"], str) and t["source"]
    assert isinstance(t["title"], str) and t["title"]
    assert isinstance(t["status"], str) and t["status"]
    for arr_field in ("conditions", "sponsors", "countries",
                      "secondaryEndpoints", "interventions", "locations"):
        assert isinstance(t[arr_field], list), f"{arr_field} must be a list"
        assert all(isinstance(x, str) for x in t[arr_field])
    for opt in ("scientificTitle", "phase", "studyType", "registrationDate",
                "startDate", "completionDate", "lastUpdated", "url",
                "primaryEndpoint"):
        assert t.get(opt) is None or isinstance(t[opt], str)
    assert t.get("enrollment") is None or isinstance(t["enrollment"], int)


def _assert_event_shape(ev) -> None:
    for req in ("detected_at", "field_name", "id", "source", "title"):
        assert isinstance(ev[req], str), f"{req} must be a string"
    for opt in ("old_value", "new_value", "change_category", "source_url"):
        assert ev.get(opt) is None or isinstance(ev[opt], str)


def _assert_generated_and_as_of(payload) -> None:
    assert isinstance(payload["generated_at"], str) and payload["generated_at"]
    as_of = payload["data_as_of"]
    assert isinstance(as_of, dict)
    assert all(isinstance(k, str) and (v is None or isinstance(v, str))
               for k, v in as_of.items())


# ── tests ─────────────────────────────────────────────────────────────────


def test_health(seeded_client):
    r = seeded_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_profiles_contract(seeded_client):
    r = seeded_client.get("/api/profiles")
    assert r.status_code == 200
    payload = r.json()
    _assert_generated_and_as_of(payload)
    keys = {d["key"] for d in payload["diseases"]}
    assert {"mi", "hf", "cmp", "mm"} <= keys
    hf = next(d for d in payload["diseases"] if d["key"] == "hf")
    for field in ("label", "label_en", "file"):
        assert isinstance(hf[field], str) and hf[field]
    assert isinstance(hf["total"], int) and hf["total"] >= 2  # NCT + ChiCTR seeds


def test_trials_profile_returns_trial_contract(seeded_client):
    r = seeded_client.get("/api/trials", params={"profile": "hf"})
    assert r.status_code == 200
    payload = r.json()
    _assert_generated_and_as_of(payload)
    assert payload["profile"] == "hf"
    assert payload["total"] == 2
    ids = {t["id"] for t in payload["trials"]}
    assert ids == {"NCT00000001", "ChiCTR2100012345"}
    for t in payload["trials"]:
        _assert_trial_shape(t)
    nct = next(t for t in payload["trials"] if t["id"] == "NCT00000001")
    assert nct["source"] == "NCT"
    assert nct["status"] == "Recruiting"
    assert nct["enrollment"] == 120
    assert nct["sponsors"] == ["Acme Medical"]
    assert nct["countries"] == ["China", "United States"]


def test_trials_unknown_profile_404(seeded_client):
    r = seeded_client.get("/api/trials", params={"profile": "zzz"})
    assert r.status_code == 404
    assert "unknown profile" in r.json()["detail"]


def test_trials_source_and_status_filters(seeded_client):
    only_nct = seeded_client.get("/api/trials", params={"profile": "hf", "source": "NCT"}).json()
    assert only_nct["total"] == 1
    assert only_nct["trials"][0]["source"] == "NCT"

    recruiting = seeded_client.get(
        "/api/trials", params={"profile": "hf", "status": "recruiting"}).json()
    assert recruiting["total"] == 2

    completed = seeded_client.get(
        "/api/trials", params={"profile": "hf", "status": "completed"}).json()
    assert completed["total"] == 0  # the completed seed is MI, not HF


def test_status_bucket_excludes_explicit_non_recruiting_labels():
    """Substring matching must not turn negated recruiting labels positive."""
    from server.app import _status_key

    assert _status_key("Recruiting") == "recruiting"
    assert _status_key("正在招募") == "recruiting"
    for label in ("Not yet recruiting", "Active, not recruiting",
                  "尚未招募", "招募暂停"):
        assert _status_key(label) == "other"


def test_trials_pagination(seeded_client):
    r = seeded_client.get(
        "/api/trials", params={"profile": "hf", "page": 2, "page_size": 1})
    payload = r.json()
    assert payload["total"] == 2          # total unaffected by paging
    assert len(payload["trials"]) == 1


def test_shared_read_only_search_has_full_facets_and_no_monitor_side_effects(seeded_client):
    before = seeded_client.get("/api/monitors").json()["monitors"]
    result = seeded_client.get("/api/trials/search", params={
        "q": "heart failure", "registries": "NCT", "statuses": "Recruiting",
        "phase": "Phase 2", "country": "United States", "page": 1, "page_size": 1,
    })
    assert result.status_code == 200
    body = result.json()
    assert body["total"] == 1 and len(body["trials"]) == 1
    assert body["facets"]["registries"] == {"NCT": 1}
    assert body["rules"] == {"query": "heart failure", "registries": ["NCT"],
                             "statuses": ["Recruiting"], "phase": ["Phase 2"],
                             "country": ["United States"]}
    assert seeded_client.get("/api/monitors").json()["monitors"] == before


def test_search_reports_matches_without_country_data(seeded_client):
    """Country facets only count records carrying recruitment-country data
    (live WHO portal rows have none); countries_missing reports the rest so
    the buckets don't read like a miscount against total."""
    from db.connection import get_connection
    conn = get_connection()
    _insert_record(conn, "NCT", "NCT00000003",
                   "Heart failure pilot without geography")
    conn.commit()

    body = seeded_client.get("/api/trials/search",
                             params={"q": "heart failure"}).json()
    # matched set: NCT00000001 (China + United States), ChiCTR2100012345
    # (中国, via bilingual expansion) + the geography-less record — the
    # latter appears in no country bucket
    assert body["total"] == 3
    assert body["facets"]["countries"] == {
        "China": 1, "United States": 1, "中国": 1}
    assert body["facets"]["countries_missing"] == 1


def test_trials_keywords_override(seeded_client):
    r = seeded_client.get(
        "/api/trials",
        params={"profile": "hf", "keywords": "myocardial infarction"})
    payload = r.json()
    assert payload["total"] == 1
    assert payload["trials"][0]["id"] == "NCT00000002"


def test_trial_detail_contract(seeded_client):
    r = seeded_client.get("/api/trials/NCT/NCT00000001")
    assert r.status_code == 200
    payload = r.json()
    _assert_generated_and_as_of(payload)
    assert payload["source"] == "NCT" and payload["id"] == "NCT00000001"
    # TrialDetailData fields (all optional but present here)
    assert payload["summary"] == "A heart failure device study."
    assert "Dr Li" in payload["investigator"]
    assert "chronic heart failure" in payload["inclusion"]
    assert "pregnancy" in payload["exclusion"]
    # per-trial change timeline in ChangeEvent shape — the WHOLE version
    # chain (event from the superseded v1 row included), oldest first
    history = payload["changeHistory"]
    assert len(history) == 3
    assert [e["detected_at"] for e in history] == sorted(e["detected_at"] for e in history)
    assert history[0]["detected_at"] == "2026-09-10 08:00:00"  # superseded row
    for ev in history:
        _assert_event_shape(ev)
    status_ev = next(e for e in history if e["field_name"] == "status_id")
    assert status_ev["old_value"] == "Not yet recruiting"   # FK translated
    assert status_ev["new_value"] == "Recruiting"
    # timeline origin: when the trial entered the monitor library
    assert payload["first_crawled_at"] == "2026-08-15 08:00:00"


def test_trial_detail_404(seeded_client):
    assert seeded_client.get("/api/trials/NCT/NCT99999999").status_code == 404
    # stale (non-latest) versions must not leak through — no such seed here,
    # but the route also 404s for wrong-source pairs
    assert seeded_client.get("/api/trials/ChiCTR/NCT00000001").status_code == 404


def test_trial_mirrors_endpoint(seeded_client):
    r = seeded_client.get("/api/trials/NCT/NCT00000001/mirrors")
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 1
    sib = payload["siblings"][0]
    assert sib["short_name"] == "ChiCTR"
    assert sib["source_trial_id"] == "ChiCTR2100012345"
    assert sib["source_url"].startswith("http")


def test_mirrors_endpoint_contract(seeded_client):
    r = seeded_client.get("/api/mirrors")
    assert r.status_code == 200
    payload = r.json()
    _assert_generated_and_as_of(payload)
    assert payload["total"] == 1
    group = payload["groups"][0]
    assert isinstance(group["masterId"], str) and group["masterId"]
    assert group["sources"] == ["ChiCTR", "NCT"]  # sorted
    for trial in group["trials"]:
        assert isinstance(trial["source"], str)
        assert isinstance(trial["id"], str)
        assert trial.get("status") is None or isinstance(trial["status"], str)
        assert trial.get("enrollment") is None or isinstance(trial["enrollment"], int)
        assert trial.get("url") is None or isinstance(trial["url"], str)


def test_events_endpoint_contract_and_profile_filter(seeded_client):
    r = seeded_client.get("/api/events")
    assert r.status_code == 200
    payload = r.json()
    _assert_generated_and_as_of(payload)
    assert payload["total"] == 3  # incl. the superseded-row event
    for ev in payload["events"]:
        _assert_event_shape(ev)

    # profile restriction drops events of trials outside the hf keywords
    hf_only = seeded_client.get("/api/events", params={"profile": "hf"}).json()
    assert {e["id"] for e in hf_only["events"]} == {"NCT00000001"}
    assert isinstance(hf_only["daily"], list)


def test_events_limit_zero_returns_complete_window(seeded_client):
    """limit=0 is the explicit unbounded mode used by the timeline UI."""
    payload = seeded_client.get(
        "/api/events", params={"days": 0, "limit": 0}).json()
    assert payload["total"] == 3
    assert len(payload["events"]) == 3


def test_events_daily_grouping(seeded_client):
    """daily = day → per-trial grouped changes (+ new registrations)."""
    payload = seeded_client.get("/api/events").json()
    daily = payload["daily"]
    dates = [d["date"] for d in daily]
    assert dates == sorted(dates, reverse=True)          # newest day first
    assert "2026-09-12" in dates                         # seeded events day
    assert "2026-09-10" in dates                         # superseded-row event
    day = next(d for d in daily if d["date"] == "2026-09-12")
    assert day["change_count"] == 2
    # both seeded events belong to one trial → one group, two field diffs
    assert len(day["trials"]) == 1
    t = day["trials"][0]
    assert (t["source"], t["id"]) == ("NCT", "NCT00000001")
    assert {c["field_name"] for c in t["changes"]} == {"status_id", "enrollment"}
    # status value translated inside daily diffs too
    st = next(c for c in t["changes"] if c["field_name"] == "status_id")
    assert st["new_value"] == "Recruiting"


def test_events_days_window_and_new_registrations(seeded_client):
    """days= windows both changes and new registrations; the digest's
    "new trial" semantics (bootstrap excluded, version 1 only) feed
    daily[].new_trials."""
    from datetime import datetime, timedelta, timezone
    from db.connection import get_connection
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    _insert_record(
        get_connection(), "NCT", "NCT00000009",
        "Fresh myocardial infarction trial", status="Recruiting",
        first_crawled=yesterday,
    )
    get_connection().commit()

    all_rows = seeded_client.get("/api/events", params={"days": 0}).json()
    assert "2026-08-15" in [d["date"] for d in all_rows["daily"]]  # seeds day

    week = seeded_client.get("/api/events", params={"days": 7}).json()
    dates = [d["date"] for d in week["daily"]]
    assert "2026-08-15" not in dates                       # outside window
    y_day = next(d for d in week["daily"] if d["date"] == yesterday[:10])
    assert y_day["new_count"] == 1
    nt = y_day["new_trials"][0]
    assert (nt["source"], nt["id"]) == ("NCT", "NCT00000009")
    assert nt["title"] == "Fresh myocardial infarction trial"


def test_profile_details_map_contract(seeded_client):
    """<key>_details.json parity: a BARE source-scoped map (no wrapper keys —
    the viewer's isDetailsMap guard rejects non-detail values)."""
    r = seeded_client.get("/api/profiles/hf/details")
    assert r.status_code == 200
    payload = r.json()
    assert set(payload) == {"NCT:NCT00000001", "ChiCTR:ChiCTR2100012345"}
    d = payload["NCT:NCT00000001"]
    assert d["summary"] == "A heart failure device study."
    assert d["endpointTable"] is None  # NCT, not CDE/CTR
    for opt in ("summary", "investigator", "inclusion", "exclusion"):
        assert d.get(opt) is None or isinstance(d[opt], str)
    assert seeded_client.get("/api/profiles/zzz/details").status_code == 404


NCT_LIVE_STUDY = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT99999999", "briefTitle": "Live HCM drug trial"},
        "statusModule": {"overallStatus": "RECRUITING",
                          "studyFirstSubmitDate": "2026-01-15",
                          "lastUpdateSubmitDate": "2026-06-01"},
        "descriptionModule": {},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Live Pharma"}},
        "designModule": {"phases": ["PHASE2"], "enrollmentInfo": {"count": 88}},
        "conditionsModule": {"conditions": ["Hypertrophic Cardiomyopathy"]},
        "contactsLocationsModule": {"locations": [{"facility": "HQ", "country": "Germany"}]},
        "outcomesModule": {"primaryOutcomes": [{"measure": "MACE at 12 months"}]},
    },
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_nct_live_proxy(monkeypatch, seeded_client):
    """Phase 7 P2: thin cached proxy to ClinicalTrials.gov API v2 — results
    parse through the collector's own normalise() and carry the Trial shape."""
    calls = []
    monkeypatch.setattr("server.app.NCT_LIVE_MIN_INTERVAL_SEC", 0)

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _FakeResp({"studies": [NCT_LIVE_STUDY]})

    monkeypatch.setattr("server.app.http_get", fake_get)
    r = seeded_client.get("/api/nct/live",
                          params={"keywords": "hypertrophic cardiomyopathy",
                                  "max_results": 5})
    assert r.status_code == 200
    payload = r.json()
    assert payload["live"] is True and payload["cached"] is False
    assert payload["total"] == 1
    t = payload["trials"][0]
    _assert_trial_shape(t)
    assert t["source"] == "NCT" and t["id"] == "NCT99999999"
    assert t["status"] == "Recruiting"           # collector status normalisation
    assert t["phase"] == "Phase 2"               # collector phase normalisation
    assert t["sponsors"] == ["Live Pharma"]
    assert t["url"] == "https://clinicaltrials.gov/study/NCT99999999"
    assert calls and calls[0][1]["query.term"] == "hypertrophic cardiomyopathy"
    assert calls[0][1]["pageSize"] == 5

    # cache: a case-insensitive repeat within the TTL must not re-fetch
    r2 = seeded_client.get(
        "/api/nct/live", params={"keywords": "Hypertrophic CARDIOMYOPATHY",
                                 "max_results": 5}).json()
    assert r2["cached"] is True
    assert len(calls) == 1

    # max_results is part of the response contract and therefore the cache
    # identity: a small first request must not truncate a later larger one.
    r3 = seeded_client.get(
        "/api/nct/live", params={"keywords": "hypertrophic cardiomyopathy",
                                 "max_results": 10}).json()
    assert r3["cached"] is False
    assert len(calls) == 2
    assert calls[-1][1]["pageSize"] == 10

    # upstream failures degrade to a clean 502
    def boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("server.app.http_get", boom)
    err = seeded_client.get("/api/nct/live", params={"keywords": "another query"})
    assert err.status_code == 502
    assert "upstream down" in err.json()["detail"]


def test_nct_live_requires_keywords(seeded_client):
    assert seeded_client.get("/api/nct/live").status_code == 422


def test_chictr_local_api_is_read_only_cached_adapter(test_db, monkeypatch):
    """Search/detail endpoints expose the collector without queue side effects."""
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)
    monkeypatch.setattr("server.app.CHICTR_API_MIN_INTERVAL_SEC", 0)

    class FakeChiCTRCollector:
        def __init__(self):
            self.search_calls = 0
            self.study_calls = 0

        def api_search(self, query, page):
            self.search_calls += 1
            return {
                "source": "ChiCTR", "query": query, "page": page,
                "page_size": 10, "site_total": 1, "has_more": False,
                "items": [{
                    "source_trial_id": "ChiCTR2600128415",
                    "title": "心力衰竭研究", "proj_id": "291119",
                    "url": "https://www.chictr.org.cn/showproj.html?proj=291119",
                }],
            }

        def api_study(self, trial_id):
            self.study_calls += 1
            if trial_id == "ChiCTR2600999999":
                return None
            return {
                "source": "ChiCTR", "source_trial_id": trial_id,
                "proj_id": "291119",
                "url": "https://www.chictr.org.cn/showproj.html?proj=291119",
                "fields": {"title": "心力衰竭研究"},
                "canonical": {"source_trial_id": trial_id,
                              "title": "心力衰竭研究"},
            }

    fake = FakeChiCTRCollector()
    monkeypatch.setattr("collectors.chictr.ChiCTRCollector", lambda: fake)

    from fastapi.testclient import TestClient
    from server.app import create_app

    with TestClient(create_app()) as client:
        search = client.get("/api/chictr/search",
                            params={"q": "心力衰竭", "page": 1})
        assert search.status_code == 200
        assert search.json()["items"][0]["source_trial_id"] == "ChiCTR2600128415"
        assert search.json()["live"] is True
        assert search.json()["cached"] is False

        cached = client.get("/api/chictr/search",
                            params={"q": "心力衰竭", "page": 1})
        assert cached.json()["cached"] is True
        assert fake.search_calls == 1

        detail = client.get("/api/chictr/studies/ChiCTR2600128415")
        assert detail.status_code == 200
        assert detail.json()["canonical"]["title"] == "心力衰竭研究"

        missing = client.get("/api/chictr/studies/ChiCTR2600999999")
        assert missing.status_code == 404
        assert client.get("/api/chictr/studies/not-an-id").status_code == 422

    # Adapter endpoints do not touch discovery_queue.
    from db.connection import get_connection
    count = get_connection().execute(
        "SELECT COUNT(*) FROM discovery_queue"
    ).fetchone()[0]
    assert count == 0


def test_ctr_local_api_is_read_only_cached_adapter(test_db, monkeypatch):
    """CTR search/detail endpoints expose the HTML adapter without writes."""
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)
    monkeypatch.setattr("server.app.CTR_API_MIN_INTERVAL_SEC", 0)

    class FakeCTRCollector:
        def __init__(self):
            self.search_calls = 0
            self.study_calls = 0

        def api_search(self, query, page, field=None):
            self.search_calls += 1
            return {
                "source": "CTR", "query": query, "query_field": field,
                "page": page, "page_size": 20, "site_total": None,
                "has_more": False,
                "items": [{
                    "source_trial_id": "CTR20262758",
                    "title": "心力衰竭药物试验",
                    "uuid": "uuid-2758", "index": "3",
                    "url": "https://www.chinadrugtrials.org.cn/detail",
                }],
            }

        def api_study(self, trial_id):
            self.study_calls += 1
            if trial_id == "CTR20999999":
                return None
            return {
                "source": "CTR", "source_trial_id": trial_id,
                "uuid": "uuid-2758", "index": "3",
                "url": "https://www.chinadrugtrials.org.cn/detail",
                "fields": {"title": "心力衰竭药物试验"},
                "canonical": {"source_trial_id": trial_id,
                              "title": "心力衰竭药物试验"},
            }

    fake = FakeCTRCollector()
    monkeypatch.setattr(
        "collectors.chinadrugtrials.ChinaDrugTrialsCollector", lambda: fake,
    )

    from fastapi.testclient import TestClient
    from server.app import create_app

    with TestClient(create_app()) as client:
        search = client.get(
            "/api/ctr/search",
            params={"q": "心力衰竭", "page": 1, "field": "indication"},
        )
        assert search.status_code == 200
        assert search.json()["items"][0]["source_trial_id"] == "CTR20262758"
        assert search.json()["cached"] is False

        cached = client.get(
            "/api/ctr/search",
            params={"q": "心力衰竭", "page": 1, "field": "indication"},
        )
        assert cached.json()["cached"] is True
        assert fake.search_calls == 1

        detail = client.get("/api/ctr/studies/ctr20262758")
        assert detail.status_code == 200
        assert detail.json()["canonical"]["title"] == "心力衰竭药物试验"

        missing = client.get("/api/ctr/studies/CTR20999999")
        assert missing.status_code == 404
        assert client.get("/api/ctr/studies/not-an-id").status_code == 422

    from db.connection import get_connection
    count = get_connection().execute(
        "SELECT COUNT(*) FROM discovery_queue"
    ).fetchone()[0]
    assert count == 0


def test_ictrp_local_study_exposes_full_xml_payload(seeded_client):
    """WHO detail reads the local mirror and labels XML vs portal scope."""
    from db.connection import get_connection

    conn = get_connection()
    xml_payload = {
        "who_ictrp_record": {
            "reg_name": "Clinical Trials Registry - India",
            "trial_id": "CTRI/2026/001",
            "public_title": "Complete WHO export record",
            "primary_outcome": "All-cause mortality at 12 months",
        },
        "_aggregator": {"is_aggregator": True},
        "secondary_ids": ["NCT01234567"],
    }
    _insert_record(
        conn, "ICTRP", "CTRI/2026/001", "Complete WHO export record",
        status="Recruiting", conditions='["Heart failure"]',
        sponsors='["Example Institute"]',
        raw_payload=json.dumps(xml_payload, ensure_ascii=False),
        source_url="https://trialsearch.who.int/Trial3.aspx?trialid=CTRI%2F2026%2F001",
    )
    portal_payload = {
        "who_ictrp_record": {
            "reg_name": "NCT", "trial_id": "NCT09999991",
            "public_title": "Portal-only record", "_portal_live": True,
        },
        "_aggregator": {"is_aggregator": True},
        "secondary_ids": [],
    }
    _insert_record(
        conn, "ICTRP", "NCT09999991", "Portal-only record",
        raw_payload=json.dumps(portal_payload, ensure_ascii=False),
    )
    conn.commit()

    # ``:path`` preserves primary-registry IDs containing slashes.
    response = seeded_client.get("/api/ictrp/studies/CTRI/2026/001")
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is False
    assert body["record_scope"] == "xml_export_record"
    assert body["complete_export_record"] is True
    assert body["canonical"]["conditions"] == ["Heart failure"]
    assert body["raw_payload"] == xml_payload

    partial = seeded_client.get("/api/ictrp/studies/NCT09999991").json()
    assert partial["record_scope"] == "portal_list_metadata"
    assert partial["complete_export_record"] is False

    missing = seeded_client.get("/api/ictrp/studies/NCT00000999")
    assert missing.status_code == 404


def test_watch_crud_and_hit_counts(seeded_client):
    """Watch subscriptions: add (dedup 409), list with live match counts,
    delete (404 on repeat).  First write endpoints — local single-user."""
    r = seeded_client.post("/api/watch", params={"keyword": "device"})
    assert r.status_code == 201
    body = r.json()
    wid = body["watch"]["watch_id"]
    assert body["watch"]["keyword"] == "device"

    # duplicate registration is rejected
    assert seeded_client.post("/api/watch", params={"keyword": "device"}).status_code == 409

    # list: live counts against the library — "device" matches the seeded
    # NCT00000001 title; its first_crawled_at is outside the 7-day window
    lst = seeded_client.get("/api/watch").json()
    assert len(lst["watches"]) == 1
    w = lst["watches"][0]
    assert w["keyword"] == "device"
    assert w["total_matches"] >= 1
    assert w["recent_hits"] == 0
    assert w["recent_matches"] == []

    # blank/short keywords are rejected
    assert seeded_client.post("/api/watch", params={"keyword": "x"}).status_code == 422

    # delete → gone; repeat delete → 404
    assert seeded_client.delete(f"/api/watch/{wid}").status_code == 200
    assert seeded_client.get("/api/watch").json()["watches"] == []
    assert seeded_client.delete(f"/api/watch/{wid}").status_code == 404


def test_trial_timeline_spans_sources(seeded_client):
    """The same trial mirrored under another registry (ICTRP reuses the
    source ID) must show the events detected on the NCT record — opening a
    mirror card must not blank the timeline."""
    from db.connection import get_connection
    conn = get_connection()
    conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, version_number, is_latest)
           VALUES ((SELECT source_id FROM registry_sources WHERE short_name='ICTRP'),
                   'NCT00000001', 'Mirror record of the same trial', 1, 1)""")
    conn.commit()

    payload = seeded_client.get("/api/trials/ICTRP/NCT00000001").json()
    assert len(payload["changeHistory"]) == 3   # the NCT chain's events
    # origin = earliest first_crawled across every registry holding the ID
    assert payload["first_crawled_at"] == "2026-08-15 08:00:00"


def test_stats_endpoint(seeded_client):
    r = seeded_client.get("/api/stats")
    assert r.status_code == 200
    payload = r.json()
    _assert_generated_and_as_of(payload)
    assert payload["records_by_source"]["NCT"] == 2
    assert payload["records_by_source"]["ChiCTR"] == 1
    assert payload["data_as_of"]["NCT"] == "2026-09-12 08:00:00"
    assert "totals" in payload["refresh_queue"]


def test_data_as_of_present_on_trials(seeded_client):
    payload = seeded_client.get("/api/trials", params={"profile": "mi"}).json()
    as_of = payload["data_as_of"]
    # every registry appears; NCT shows its sync_status stamp (newer than the
    # crawl), ChiCTR falls back to its newest last_crawled_at (no sync row)
    assert as_of["NCT"] == "2026-09-12 08:00:00"
    assert as_of["ChiCTR"] == "2026-09-01 00:00:00"
    assert set(as_of) >= {"NCT", "ChiCTR", "CTR", "ICTRP"}


def test_events_status_fk_translated_in_global_stream(seeded_client):
    """P0 fix: the global timeline must show status labels, not bare FK ints."""
    payload = seeded_client.get("/api/events").json()
    status_ev = next(e for e in payload["events"] if e["field_name"] == "status_id")
    assert status_ev["old_value"] == "Not yet recruiting"
    assert status_ev["new_value"] == "Recruiting"


def test_trial_detail_has_endpoint_table_key(seeded_client):
    """Per-trial detail is a full TrialDetailData — endpointTable key present
    (None for non-CDE sources) so the modal can render CDE endpoint tables."""
    payload = seeded_client.get("/api/trials/NCT/NCT00000001").json()
    assert "endpointTable" in payload
    assert payload["endpointTable"] is None


def test_cache_invalidated_by_external_write(seeded_client):
    """Pipeline-style writes from another connection must invalidate the
    process-local cache (data_version check), never serve stale counts."""
    first = seeded_client.get("/api/trials", params={"profile": "mi"}).json()
    assert first["total"] == 1

    from db.connection import get_connection
    _insert_record(
        get_connection(), "NCT", "NCT00000003",
        "Acute myocardial infarction follow-up registry",
        status="Recruiting",
    )
    get_connection().commit()

    second = seeded_client.get("/api/trials", params={"profile": "mi"}).json()
    assert second["total"] == 2

    # and the catalogue totals ride the same cache entries
    hf_total = next(d for d in seeded_client.get("/api/profiles").json()["diseases"]
                    if d["key"] == "mi")
    assert hf_total["total"] == 2


def test_trials_response_is_gzipped(seeded_client):
    r = seeded_client.get(
        "/api/trials", params={"profile": "hf"},
        headers={"Accept-Encoding": "gzip"},
    )
    assert r.headers.get("content-encoding") == "gzip"


# ── Phase 3E: health capability, dashboard aggregate, labels, SPA fallback ─


def test_health_reports_schema_version(seeded_client):
    r = seeded_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["schema_version"], int) and body["schema_version"] >= 12


def test_dashboard_endpoint_shape_and_counts(seeded_client):
    # Fixture events are fixed historical dates; use the deterministic 30-day
    # reporting window rather than allowing wall-clock drift to invalidate a
    # shape/count contract.
    r = seeded_client.get("/api/dashboard", params={"window": "30d"})
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == "30d"
    assert set(body["summary"]) == {
        "new_trials", "updated_trials", "important_changes", "watched_updates"}
    # the seeded fixture has enrollment + status events on NCT00000001
    assert body["summary"]["updated_trials"] >= 1
    assert len(body["updates"]) >= 2
    update = body["updates"][0]
    assert update["field_label"]
    assert update["field_label_zh"]
    assert update["severity"] in {"critical", "important", "normal", "minor"}
    assert isinstance(body["monitors"], list)
    assert isinstance(body["watched_activity"], list)
    assert body["data_as_of"]


def test_dashboard_windows_validated(seeded_client):
    for win in ("24h", "7d", "30d"):
        assert seeded_client.get("/api/dashboard", params={"window": win}).status_code == 200
    assert seeded_client.get("/api/dashboard", params={"window": "10d"}).status_code == 422


def test_dashboard_counts_watched_updates(seeded_client):
    from db.connection import get_connection

    conn = get_connection()
    conn.execute(
        "INSERT INTO trial_watches (trial_id, enabled) VALUES ('NCT00000001', 1)")
    conn.commit()

    body = seeded_client.get("/api/dashboard", params={"window": "30d"}).json()
    assert body["summary"]["watched_updates"] >= 1
    activity = next(w for w in body["watched_activity"] if w["trial_id"] == "NCT00000001")
    assert activity["change_count"] >= 1
    assert "NCT" in activity["registries"]


def test_trial_changes_carry_labels_and_translated_status(seeded_client):
    r = seeded_client.get("/api/trials/NCT/NCT00000001/changes")
    assert r.status_code == 200
    events = r.json()["events"]
    assert events
    for ev in events:
        assert ev["field_label"], ev["field_name"]
        assert ev["field_label_zh"]
    status_events = [e for e in events if e["field_name"] == "status_id"]
    assert status_events, "fixture must contain a status event"
    for ev in status_events:
        # FK values must be translated to labels, never raw integers
        for v in (ev["old_value"], ev["new_value"]):
            if v is not None:
                assert not v.isdigit(), f"raw status FK leaked: {v}"


def test_events_stream_carry_labels(seeded_client):
    body = seeded_client.get("/api/events", params={"limit": 10}).json()
    assert body["events"]
    assert body["events"][0]["field_label"]


def test_scoped_updates_are_event_id_stable_and_deduplicated(seeded_client):
    """Updates reads the persisted trial-event chain, not copied monitor rows."""
    all_rows = seeded_client.get("/api/updates", params={"scope": "all", "window": "all"})
    assert all_rows.status_code == 200
    body = all_rows.json()
    assert body["total"] == len({row["event_id"] for row in body["items"]})
    assert sum(body["analytics"]["categories"].values()) == body["total"]
    assert sum(sum(day[severity] for severity in ("critical", "important", "normal", "minor"))
               for day in body["analytics"]["trends"]) == body["total"]
    assert body["items"]
    first = body["items"][0]
    assert {"event_id", "trial_id", "source", "old_value", "new_value", "severity", "field_label"} <= set(first)
    from core.intelligence import FIELD_CATEGORIES
    category = FIELD_CATEGORIES.get(first["field_name"], first["change_category"] or "other")
    category_rows = seeded_client.get("/api/updates", params={"scope": "all", "window": "all", "category": category}).json()
    assert category_rows["total"] >= 1
    assert set(category_rows["analytics"]["categories"]) == {category}
    assert first["event_id"] in {row["event_id"] for row in category_rows["items"]}

    watched = seeded_client.post("/api/trials/NCT/NCT00000001/watch")
    assert watched.status_code == 200
    watched_rows = seeded_client.get("/api/updates", params={"scope": "watched", "window": "all"}).json()
    assert watched_rows["items"]
    assert all(row["trial_id"] == "NCT00000001" and row["watched"] for row in watched_rows["items"])
    critical = seeded_client.get("/api/updates", params={"scope": "all", "window": "all", "severity": "critical"}).json()
    assert critical["total"] == sum(critical["analytics"]["categories"].values())
    assert all(day["important"] == day["normal"] == day["minor"] == 0 for day in critical["analytics"]["trends"])
    priority = seeded_client.get("/api/updates", params={"scope": "all", "window": "all", "severity": "priority"}).json()
    assert priority["total"] == body["facets"]["severities"].get("critical", 0) + body["facets"]["severities"].get("important", 0)
    assert all(day["normal"] == day["minor"] == 0 for day in priority["analytics"]["trends"])


def test_trial_detail_includes_light_trial_and_monitors(seeded_client):
    body = seeded_client.get("/api/trials/NCT/NCT00000001").json()
    trial = body["trial"]
    assert trial["id"] == "NCT00000001"
    assert trial["source"] == "NCT"
    assert trial["title"]
    assert isinstance(trial["locations"], list)
    assert isinstance(body["monitors"], list)
    assert "changeHistory" in body


def test_version_compare_endpoint_works(seeded_client):
    """Regression: compare must not 500 on tracked fields with no dedicated
    record column (e.g. contacts) — caught first by the browser E2E suite."""
    r = seeded_client.get("/api/trials/NCT/NCT00000001/versions/2/compare/1")
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert changes, "v1->v2 must differ (enrollment 100 -> 120)"
    enrollment = next(c for c in changes if c["field_name"] == "enrollment")
    assert enrollment["new_value"] == 120
    assert enrollment["change_type"] == "added"  # NULL -> 120 on the v2 fixture row


def test_spa_history_fallback_serves_index(seeded_client):
    """Deep links (e.g. /monitors) must serve the SPA, not 404 — the root
    cause of broken reloads before Phase 3E.  Skipped where the frontend
    has not been built (dist absent)."""
    from pathlib import Path

    dist_index = Path(__file__).resolve().parent.parent / "web" / "dist" / "index.html"
    if not dist_index.exists():
        pytest.skip("web/dist/index.html not built")
    r = seeded_client.get("/monitors")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="root"' in r.text
    # deep nested route too
    deep = seeded_client.get("/trials/NCT/NCT00000001")
    assert deep.status_code == 200
    # unknown API paths still 404 (fallback must never swallow the API)
    api_miss = seeded_client.get("/api/definitely-not-a-route")
    assert api_miss.status_code == 404


def test_intelligence_api_family_is_structured_and_validates_filters(seeded_client):
    overview = seeded_client.get("/api/intelligence/overview", params={"scope": "all", "window": "30d"})
    assert overview.status_code == 200
    body = overview.json()
    assert body["counts"]["change_events"] >= body["counts"]["changed_trials"]
    assert body["trends"] and "freshness" in body
    briefing = seeded_client.get("/api/intelligence/briefing", params={"scope": "all", "window": "30d"})
    assert briefing.status_code == 200
    assert briefing.json()["counts"] == body["counts"]
    assert briefing.json()["sections"]
    trends = seeded_client.get("/api/intelligence/trends", params={"scope": "all", "window": "30d"})
    assert trends.status_code == 200 and trends.json()["buckets"]
    monitors = seeded_client.get("/api/intelligence/monitors", params={"window": "30d"})
    assert monitors.status_code == 200 and isinstance(monitors.json()["monitors"], list)
    assert seeded_client.get("/api/intelligence/overview", params={"scope": "monitor", "monitor_id": 999}).status_code == 404
    assert seeded_client.get("/api/intelligence/overview", params={"scope": "monitor"}).status_code == 422
    assert seeded_client.get("/api/intelligence/overview", params={"scope": "bogus"}).status_code == 422
    assert seeded_client.get("/api/intelligence/overview", params={"registry": "bogus"}).status_code == 422
