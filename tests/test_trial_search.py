"""Phase 3E.1 — read-only cross-registry trial search + search-to-monitor.

Search must behave like an unsaved monitor preview: one shared evaluator
(`core.monitors.matching_trials`) powers `/api/trials/search`, monitor
preview and saved-monitor matching.  These tests pin:

* the search contract (light Trial shape, pagination, sort, facets);
* rule semantics for every structured filter dimension;
* search is strictly read-only (no monitors / memberships / events /
  notifications as a side effect of searching);
* a monitor saved from a search tracks the RULE: current matches become a
  quiet baseline, future matching trials enter, non-matching ones leave.
"""
from __future__ import annotations

import pytest

from tests.test_server_api import _insert_record


@pytest.fixture
def search_client(test_db, monkeypatch):
    """Temp schema + a four-registry myocarditis/heart-failure corpus."""
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    from db.connection import get_connection
    conn = get_connection()

    _insert_record(
        conn, "NCT", "NCT09000001", "Myocarditis CAR-T therapy trial",
        sci="CAR-T cells in myocarditis", status="Recruiting", phase="Phase 1/Phase 2",
        enrollment=60, conditions='["Myocarditis"]', sponsors='["Acme Biotech"]',
        countries='["United States"]', interventions='["Drug: CAR-T"]',
        source_url="https://clinicaltrials.gov/study/NCT09000001",
    )
    conn.execute("UPDATE registry_records SET last_updated_at_source='2026-09-10' "
                 "WHERE source_trial_id='NCT09000001'")
    _insert_record(
        conn, "NCT", "NCT09000002", "Myocarditis imaging study",
        status="Recruiting", phase="Phase 2", enrollment=200,
        conditions='["Myocarditis"]', sponsors='["Imaging Corp"]',
        countries='["United States", "China"]',
    )
    conn.execute("UPDATE registry_records SET last_updated_at_source='2026-09-01' "
                 "WHERE source_trial_id='NCT09000002'")
    _insert_record(
        conn, "NCT", "NCT09000003", "Chronic myocarditis observation registry",
        status="Completed", phase="N/A", enrollment=500,
        conditions='["Myocarditis"]', sponsors='["Registry Org"]',
        countries='["China"]',
    )
    conn.execute("UPDATE registry_records SET last_updated_at_source='2026-08-20' "
                 "WHERE source_trial_id='NCT09000003'")
    # the shared _insert_record helper never sets study_type_id — the one
    # observational trial gets its normalized study type explicitly
    conn.execute("UPDATE registry_records SET study_type_id="
                 "(SELECT study_type_id FROM study_types WHERE label='Observational') "
                 "WHERE source_trial_id='NCT09000003'")
    _insert_record(
        conn, "ChiCTR", "ChiCTR2200060001", "心肌炎免疫机制研究",
        sci="Immune mechanisms of myocarditis",
        status="Recruiting", phase="Phase 2", enrollment=150,
        conditions='["心肌炎"]', sponsors='["北京协和医院"]', countries='["中国"]',
        source_url="https://www.chictr.org.cn/showproj.html?proj=ChiCTR2200060001",
    )
    conn.execute("UPDATE registry_records SET last_updated_at_source='2026-09-05' "
                 "WHERE source_trial_id='ChiCTR2200060001'")
    _insert_record(
        conn, "ICTRP", "ICTRP-MIR-0001", "Myocarditis WHO mirror record",
        status="Recruiting", phase="Phase 3", enrollment=90,
        conditions='["Myocarditis"]', sponsors='["WHO Site"]',
        countries='["United Kingdom"]',
    )
    # heart-failure control trial: must never appear in myocarditis searches
    _insert_record(
        conn, "NCT", "NCT09000004", "Heart failure device trial",
        status="Recruiting", phase="Phase 3", enrollment=300,
        conditions='["Heart Failure"]', sponsors='["Acme Medical"]',
        countries='["United States"]',
    )
    conn.commit()

    from fastapi.testclient import TestClient
    from server.app import create_app

    yield TestClient(create_app())


def _search(client, **params):
    return client.get("/api/trials/search", params=params)


ALL_MYOCARDITIS = {"NCT09000001", "NCT09000002", "NCT09000003",
                   "ChiCTR2200060001", "ICTRP-MIR-0001"}


# ── search contract ────────────────────────────────────────────────────────


class TestSearchAPI:
    def test_keyword_query_returns_expected_trials(self, search_client):
        r = _search(search_client, q="myocarditis")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 5
        assert {t["id"] for t in body["trials"]} == ALL_MYOCARDITIS
        # every row is a full light Trial (shape mirrored from guards.ts)
        for t in body["trials"]:
            assert t["source"] and t["title"]
            for arr in ("conditions", "sponsors", "countries", "interventions"):
                assert isinstance(t[arr], list)
        # normalized status label surfaces (not "Unknown")
        assert {t["status"] for t in body["trials"] if t["id"] == "NCT09000001"} == {"Recruiting"}

    def test_query_is_case_insensitive_and_shared_with_monitors(self, search_client):
        assert _search(search_client, q="Myocarditis").json()["total"] == 5
        # the same rule via monitor preview must agree exactly
        preview = search_client.post("/api/monitors/preview",
                                     json={"rules": {"query": "Myocarditis"}}).json()
        assert preview["matched_count"] == 5

    def test_multi_word_query_is_and_of_words(self, search_client):
        # "myocarditis car-t" matches only trials mentioning both words;
        # "heart failure" (control trial) never enters a myocarditis search
        assert {t["id"] for t in _search(search_client, q="myocarditis car-t").json()["trials"]} \
            == {"NCT09000001"}
        assert _search(search_client, q="car-t heart failure").json()["total"] == 0

    def test_registry_filter(self, search_client):
        body = _search(search_client, q="myocarditis", registries="ChiCTR").json()
        assert body["total"] == 1
        assert body["trials"][0]["id"] == "ChiCTR2200060001"
        assert body["trials"][0]["source"] == "ChiCTR"

    def test_multi_registry_filter(self, search_client):
        body = _search(search_client, q="myocarditis", registries="ChiCTR,ICTRP").json()
        assert {t["id"] for t in body["trials"]} == {"ChiCTR2200060001", "ICTRP-MIR-0001"}

    def test_status_filter(self, search_client):
        body = _search(search_client, q="myocarditis", statuses="Completed").json()
        assert {t["id"] for t in body["trials"]} == {"NCT09000003"}

    def test_phase_filter(self, search_client):
        # matcher semantics are substring contains, so "Phase 2" also matches
        # "Phase 1/Phase 2" — the same behaviour a saved monitor shows
        body = _search(search_client, q="myocarditis", phase="Phase 2").json()
        assert {t["id"] for t in body["trials"]} == {"NCT09000001", "NCT09000002", "ChiCTR2200060001"}

    def test_study_type_filter(self, search_client):
        body = _search(search_client, q="myocarditis", study_types="Observational").json()
        assert {t["id"] for t in body["trials"]} == {"NCT09000003"}

    def test_country_filter(self, search_client):
        body = _search(search_client, q="myocarditis", country="United States").json()
        assert {t["id"] for t in body["trials"]} == {"NCT09000001", "NCT09000002"}

    def test_combined_filters(self, search_client):
        body = _search(search_client, q="myocarditis", statuses="Recruiting",
                       phase="Phase 2", country="United States",
                       registries="NCT").json()
        assert {t["id"] for t in body["trials"]} == {"NCT09000001", "NCT09000002"}
        assert body["rules"] == {"query": "myocarditis", "registries": ["NCT"],
                                 "statuses": ["Recruiting"], "phase": ["Phase 2"],
                                 "country": ["United States"]}

    def test_pagination(self, search_client):
        p1 = _search(search_client, q="myocarditis", page=1, page_size=2).json()
        p2 = _search(search_client, q="myocarditis", page=2, page_size=2).json()
        p3 = _search(search_client, q="myocarditis", page=3, page_size=2).json()
        assert p1["total"] == p2["total"] == p3["total"] == 5
        ids1 = {t["id"] for t in p1["trials"]}
        ids2 = {t["id"] for t in p2["trials"]}
        ids3 = {t["id"] for t in p3["trials"]}
        assert len(ids1) == len(ids2) == 2 and len(ids3) == 1
        assert not (ids1 & ids2) and not (ids1 & ids3) and not (ids2 & ids3)

    def test_pagination_reuses_one_expensive_result_set(self, search_client, monkeypatch):
        """Changing pages must only slice the cached ordered result set.

        This guards the export path too: fetching many pages should not rerun
        FTS matching, bilingual expansion and facet construction per page.
        """
        import core.monitors as monitors

        real = monitors.matching_trials
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(monitors, "matching_trials", counted)
        params = {"registries": "NCT", "sort": "id", "page_size": 2}
        assert _search(search_client, page=1, **params).status_code == 200
        assert _search(search_client, page=2, **params).status_code == 200
        assert calls == 1

    def test_sort_modes(self, search_client):
        order = [t["id"] for t in _search(search_client, q="myocarditis").json()["trials"]]
        assert order[0] == "NCT09000001"  # newest source update first
        by_id = [t["id"] for t in _search(search_client, q="myocarditis", sort="id").json()["trials"]]
        assert by_id == sorted(by_id)
        by_start = _search(search_client, q="myocarditis", sort="start_date").json()
        assert by_start["sort"] == "start_date"

    def test_registry_facet_counts_describe_full_set(self, search_client):
        body = _search(search_client, q="myocarditis")
        facets = body.json()["facets"]
        assert sum(facets["registries"].values()) == body.json()["total"]
        assert facets["registries"] == {"NCT": 3, "ChiCTR": 1, "ICTRP": 1}
        assert facets["statuses"] == {"Recruiting": 4, "Completed": 1}
        # facet counts do not describe just one page
        p1 = _search(search_client, q="myocarditis", page_size=2).json()
        assert sum(p1["facets"]["registries"].values()) == p1["total"] == 5

    def test_country_facet_counts_every_country(self, search_client):
        facets = _search(search_client, q="myocarditis").json()["facets"]
        assert facets["countries"]["United States"] == 2
        assert facets["countries"]["China"] == 2  # NCT09000002 + NCT09000003
        assert facets["countries"]["中国"] == 1

    def test_zero_results(self, search_client):
        body = _search(search_client, q="pancreatitiszz").json()
        assert body["total"] == 0 and body["trials"] == []
        assert body["unfiltered_total"] is None

    def test_zero_results_from_filters_report_unfiltered_total(self, search_client):
        # myocarditis exists globally, but no ChiCTR record is Completed —
        # the response must not imply the topic has no trials anywhere (#44)
        body = _search(search_client, q="myocarditis",
                       registries="ChiCTR", statuses="Completed").json()
        assert body["total"] == 0
        assert body["unfiltered_total"] == 5

    def test_search_without_query_lists_all_latest_records(self, search_client):
        body = _search(search_client).json()
        assert body["total"] == 6  # the whole corpus, unfiltered

    def test_unknown_sort_rejected(self, search_client):
        assert _search(search_client, sort="relevance").status_code == 422


class TestSearchReadOnly:
    """#1/#26: a search is exploration — it must never mutate monitor state."""

    def test_repeated_search_creates_no_state(self, search_client):
        from db.connection import get_connection

        def snapshot():
            conn = get_connection()
            return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("monitors", "monitor_trials", "monitor_events",
                                       "monitor_runs", "trial_watches", "notifications",
                                       "watch_keywords"))

        before = snapshot()
        for _ in range(3):
            assert _search(search_client, q="myocarditis").status_code == 200
            assert _search(search_client, q="myocarditis", statuses="Recruiting").status_code == 200
            assert search_client.post(
                "/api/monitors/preview", json={"rules": {"query": "myocarditis"}}).status_code == 200
        assert snapshot() == before

    def test_search_endpoint_executes_no_write_statements(self, search_client, monkeypatch):
        """Direct proof: a SQLite authorizer attached to the search path's
        own connection sees zero INSERT/UPDATE/DELETE actions."""
        import sqlite3

        import server.app as appmod

        write_actions = tuple(
            getattr(sqlite3, name) for name in ("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE")
            if hasattr(sqlite3, name)
        )
        if not write_actions:
            pytest.skip("sqlite3 authorizer constants unavailable")
        denied = []

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action in write_actions:
                denied.append((action, arg1))
            return sqlite3.SQLITE_OK

        real_open = appmod._open_conn

        def open_with_authorizer():
            conn = real_open()
            conn.set_authorizer(authorizer)
            return conn

        monkeypatch.setattr(appmod, "_open_conn", open_with_authorizer)
        r = _search(search_client, q="myocarditis", statuses="Recruiting")
        assert r.status_code == 200
        assert r.json()["total"] == 4
        assert denied == [], f"search performed write actions: {denied}"


# ── search → monitor ───────────────────────────────────────────────────────


class TestSearchToMonitor:
    def _track(self, client, rules, name="Myocarditis"):
        return client.post("/api/monitors", json={"name": name, "rules": rules})

    def test_saved_monitor_tracks_rule_and_baselines_quietly(self, search_client):
        rules = {"query": "myocarditis", "statuses": ["Recruiting"]}
        expected = {t["id"] for t in _search(search_client, q="myocarditis",
                                             statuses="Recruiting").json()["trials"]}
        created = self._track(search_client, rules)
        assert created.status_code == 200
        mid = created.json()["id"]
        assert created.json()["summary"]["new_count"] == 0

        persisted = search_client.get(f"/api/monitors/{mid}").json()["monitor"]
        assert persisted["rules"] == rules  # exact semantics, nothing dropped

        members = {r["trial_id"] for r in search_client.get(
            f"/api/monitors/{mid}/trials").json()["trials"]}
        assert members == expected  # current results == baseline membership

        activity = search_client.get(f"/api/monitors/{mid}/activity").json()["activity"]
        assert activity == []  # baseline is silent — no entered activity

        assert search_client.get("/api/monitors").json()["monitors"][0]["new_count"] == 0

    def test_future_matching_trial_enters(self, search_client):
        from db.connection import get_connection

        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO notification_settings (key,value) "
                     "VALUES ('activation_at', '2000-01-01')")
        conn.commit()

        mid = self._track(search_client, {"query": "myocarditis",
                                          "statuses": ["Recruiting"]}).json()["id"]
        # trial C exists but does not satisfy the rule yet
        _insert_record(
            conn, "NCT", "NCT09000009", "Myocarditis rehabilitation study",
            status="Completed", phase="Phase 2", enrollment=80,
            conditions='["Myocarditis"]', sponsors='[" Rehab Org "]',
            countries='["United States"]',
        )
        conn.commit()
        assert search_client.post(f"/api/monitors/{mid}/run").json()["summary"]["new_count"] == 0

        # C changes: now Recruiting → the RULE admits it
        conn.execute(
            "UPDATE registry_records SET status_id="
            "(SELECT status_type_id FROM status_types WHERE label='Recruiting') "
            "WHERE source_trial_id='NCT09000009' AND is_latest=1")
        conn.commit()
        summary = search_client.post(f"/api/monitors/{mid}/run").json()["summary"]
        assert summary["new_count"] == 1 and summary["matched_count"] == 5

        activity = search_client.get(f"/api/monitors/{mid}/activity").json()["activity"]
        entered = [a for a in activity if a["event_type"] == "trial_entered"]
        assert [a["trial_id"] for a in entered] == ["NCT09000009"]
        # entering a monitor generates its notification per existing settings
        notifications = search_client.get("/api/notifications").json()["notifications"]
        assert any(n["trial_id"] == "NCT09000009" and n["event_type"] == "trial_entered"
                   for n in notifications)

    def test_trial_leaving_rule_leaves_monitor(self, search_client):
        from db.connection import get_connection

        conn = get_connection()
        mid = self._track(search_client, {"query": "myocarditis",
                                          "statuses": ["Recruiting"]}).json()["id"]
        conn.execute(
            "UPDATE registry_records SET status_id="
            "(SELECT status_type_id FROM status_types WHERE label='Completed') "
            "WHERE source_trial_id='NCT09000001' AND is_latest=1")
        conn.commit()
        summary = search_client.post(f"/api/monitors/{mid}/run").json()["summary"]
        assert summary["left_count"] == 1
        activity = search_client.get(f"/api/monitors/{mid}/activity").json()["activity"]
        assert [a["event_type"] for a in activity] == ["trial_left"]
        assert activity[0]["trial_id"] == "NCT09000001"
        members = {r["trial_id"]: r for r in search_client.get(
            f"/api/monitors/{mid}/trials").json()["trials"]}
        assert members["NCT09000001"]["currently_matches"] in (0, False)

    def test_repeated_preview_and_search_cause_no_activity(self, search_client):
        mid = self._track(search_client, {"query": "myocarditis"}).json()["id"]
        for _ in range(3):
            _search(search_client, q="myocarditis")
            search_client.post("/api/monitors/preview", json={"rules": {"query": "myocarditis"}})
        activity = search_client.get(f"/api/monitors/{mid}/activity").json()["activity"]
        assert activity == []

    def test_monitor_rule_round_trip_via_search_semantics(self, search_client):
        """Search state → create payload → persisted monitor → preview agrees."""
        payload = {"query": "myocarditis", "registries": ["NCT", "ICTRP"],
                   "statuses": ["Recruiting"], "phase": ["Phase 2"],
                   "country": ["United States"]}
        search_body = _search(search_client, q="myocarditis", registries="NCT,ICTRP",
                              statuses="Recruiting", phase="Phase 2",
                              country="United States").json()
        mid = self._track(search_client, payload).json()["id"]
        persisted = search_client.get(f"/api/monitors/{mid}").json()["monitor"]["rules"]
        assert persisted == payload
        preview = search_client.post("/api/monitors/preview", json={"rules": persisted}).json()
        assert preview["matched_count"] == search_body["total"]

    def test_mirror_records_do_not_fan_out_monitor_membership_view(self, search_client):
        """#39: an NCT trial mirrored in ICTRP keeps ONE membership row, and the
        display joins pick the native record (NCT), never the aggregator copy."""
        from db.connection import get_connection

        conn = get_connection()
        _insert_record(
            conn, "NCT", "NCT09000005", "Myocarditis mirrored trial",
            status="Recruiting", phase="Phase 2", enrollment=100,
            conditions='["Myocarditis"]', sponsors='["Native Org"]',
            countries='["United States"]',
        )
        _insert_record(
            conn, "ICTRP", "NCT09000005", "Myocarditis mirrored trial",
            status="Recruiting", phase="Phase 2", enrollment=100,
            conditions='["Myocarditis"]', sponsors='["Mirror Org"]',
            countries='["Germany"]',
        )
        conn.commit()
        mid = self._track(search_client, {"query": "myocarditis"}).json()["id"]

        rows = search_client.get(f"/api/monitors/{mid}/trials").json()["trials"]
        mirrored = [r for r in rows if r["trial_id"] == "NCT09000005"]
        assert len(mirrored) == 1, "membership row fanned out across mirror sources"
        assert mirrored[0]["source"] == "NCT"
