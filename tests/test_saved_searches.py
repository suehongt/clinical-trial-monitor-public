"""Phase 3I passive query library, strict state, and monitor separation."""
import json

import pytest

from core.saved_searches import validate_state

pytest_plugins = ("tests.test_trial_search",)

STATE = {"q": "myocarditis", "statuses": ["Recruiting"], "phase": ["Phase 2"],
         "country": ["China"], "sort": "last_updated"}


def create(client, **kwargs):
    return client.post("/api/saved-searches", json={"name": "Myocarditis recruiting", "state": STATE, **kwargs})


def test_crud_pin_duplicate_search_filter_and_open_timestamp(search_client):
    a = create(search_client)
    b = create(search_client)
    assert a.status_code == b.status_code == 201
    sid, other = a.json()["saved_search"]["id"], b.json()["saved_search"]["id"]
    assert sid != other
    listing = search_client.get("/api/saved-searches", params={"q": "Myocarditis"}).json()
    assert listing["total"] == 2 and all(x["last_opened_at"] is None for x in listing["items"])
    assert search_client.get("/api/saved-searches", params={"page_size": 1, "page": 2}).json()["page"] == 2
    assert search_client.get(f"/api/saved-searches/{sid}").json()["saved_search"]["state"] == STATE
    patched = search_client.patch(f"/api/saved-searches/{sid}", json={"name": "Renamed", "description": "Research", "pinned": True})
    assert patched.status_code == 200 and patched.json()["saved_search"]["pinned"] is True
    assert search_client.get("/api/saved-searches", params={"pinned": True}).json()["total"] == 1
    assert search_client.get("/api/saved-searches").json()["items"][0]["id"] == sid
    assert search_client.post(f"/api/saved-searches/{sid}/open").json()["saved_search"]["last_opened_at"]
    assert search_client.delete(f"/api/saved-searches/{sid}").status_code == 200
    assert search_client.get(f"/api/saved-searches/{sid}").status_code == 404
    assert search_client.get(f"/api/saved-searches/{other}").status_code == 200


@pytest.mark.parametrize("payload", [
    {"name": "", "state": STATE}, {"name": "x", "state": {}},
    {"name": "x", "state": {"q": "myocarditis", "unknown": ["x"]}},
    {"name": "x", "state": {"q": "myocarditis", "page": 3}},
    {"name": "x", "state": {"q": "myocarditis", "statuses": ["Unknown"]}},
    {"name": "x", "state": {"q": "myocarditis", "sort": "unknown"}},
    {"name": "x", "state": STATE, "search_schema_version": 99},
    {"name": "x", "state": STATE, "pinned": "yes"},
    {"name": "x", "state": STATE, "extra": "surprise"},
])
def test_invalid_create_is_rejected(search_client, payload):
    assert search_client.post("/api/saved-searches", json=payload).status_code == 422
    assert search_client.get("/api/saved-searches").json()["total"] == 0


def test_invalid_update_missing_id_and_stale_schema(search_client):
    sid = create(search_client).json()["saved_search"]["id"]
    assert search_client.patch(f"/api/saved-searches/{sid}", json={"state": {"q": "x", "phase": ["Phase 8"]}}).status_code == 422
    assert search_client.patch(f"/api/saved-searches/{sid}", json={"pinned": 1}).status_code == 422
    assert search_client.patch("/api/saved-searches/999", json={"name": "Missing"}).status_code == 404
    assert search_client.delete("/api/saved-searches/999").status_code == 404
    from db.connection import get_connection
    conn = get_connection()
    conn.execute("UPDATE saved_searches SET search_schema_version=99 WHERE id=?", (sid,))
    conn.commit()
    assert search_client.get(f"/api/saved-searches/{sid}").status_code == 409
    assert search_client.post(f"/api/saved-searches/{sid}/open").status_code == 409


def test_passive_reopen_uses_current_records_without_events(search_client):
    from db.connection import get_connection
    conn = get_connection()
    tables = ("monitors", "monitor_trials", "monitor_events", "notifications", "notification_delivery_attempts")
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    sid = create(search_client).json()["saved_search"]["id"]
    assert search_client.get("/api/saved-searches").json()["total"] == 1
    saved = search_client.post(f"/api/saved-searches/{sid}/open").json()["saved_search"]
    assert saved["state"] == STATE
    assert before == {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    # Upstream changes alter current results on open, not the passive object.
    query = {"q": "myocarditis", "statuses": "Recruiting", "phase": "Phase 2", "country": "China"}
    first = search_client.get("/api/trials/search", params=query).json()["total"]
    conn.execute("UPDATE registry_records SET status_id=(SELECT status_type_id FROM status_types WHERE label='Recruiting') WHERE source_trial_id='NCT09000003'")
    conn.execute("UPDATE registry_records SET study_phase='Phase 2' WHERE source_trial_id='NCT09000003'")
    conn.commit()
    reopened = search_client.post(f"/api/saved-searches/{sid}/open").json()["saved_search"]
    assert reopened["state"] == STATE
    assert search_client.get("/api/trials/search", params=query).json()["total"] == first + 1
    assert before == {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_final_edited_rule_and_nl_provenance_are_independent(search_client, monkeypatch):
    from core.query_interpreter import interpret
    proposal = interpret("中国正在招募的II期心肌炎临床试验")
    final = {"q": "心肌炎", "statuses": ["Recruiting"], "phase": ["Phase 2"],
             "country": ["China"], "registries": ["ChiCTR"], "sort": "id"}
    sid = create(search_client, state=final, original_nl_query=proposal["original_query"],
                 interpreter_version=proposal["version"]).json()["saved_search"]["id"]
    monkeypatch.setattr("core.query_interpreter.interpret", lambda _: {"rule": {"q": "other"}})
    saved = search_client.post(f"/api/saved-searches/{sid}/open").json()["saved_search"]
    assert saved["state"] == final
    assert saved["original_nl_query"] == proposal["original_query"]
    assert saved["interpreter_version"] == proposal["version"]


def test_saved_search_to_monitor_exact_rule_quiet_baseline_and_future_match(search_client):
    from db.connection import get_connection
    conn = get_connection()
    sid = create(search_client).json()["saved_search"]["id"]
    state = search_client.post(f"/api/saved-searches/{sid}/open").json()["saved_search"]["state"]
    rule = {"query": [state["q"]], "statuses": state["statuses"],
            "phase": state["phase"], "country": state["country"]}
    preview = search_client.post("/api/monitors/preview", json={"rules": rule}).json()
    created = search_client.post("/api/monitors", json={"name": "Track saved", "rules": rule}).json()
    mid = created["id"]
    assert created["summary"]["matched_count"] == preview["matched_count"]
    assert json.loads(conn.execute("SELECT rules_json FROM monitor_rules WHERE monitor_id=?", (mid,)).fetchone()[0]) == rule
    assert conn.execute("SELECT count(*) FROM monitor_events WHERE monitor_id=?", (mid,)).fetchone()[0] == 0
    assert search_client.get(f"/api/saved-searches/{sid}").status_code == 200
    conn.execute("UPDATE registry_records SET status_id=(SELECT status_type_id FROM status_types WHERE label='Recruiting'), study_phase='Phase 2' WHERE source_trial_id='NCT09000003'")
    conn.commit()
    assert search_client.post(f"/api/monitors/{mid}/run").json()["summary"]["new_count"] == 1
    assert conn.execute("SELECT count(*) FROM monitor_events WHERE monitor_id=? AND event_type='trial_entered'", (mid,)).fetchone()[0] == 1


def test_v16_upgrade_to_v17_is_idempotent_and_does_not_import_monitors(test_db):
    from db.connection import get_connection
    from db.schema import create_schema
    conn = get_connection()
    conn.execute("DROP TABLE saved_searches")
    conn.execute("DELETE FROM schema_version WHERE version>=17")
    conn.commit()
    create_schema()
    create_schema()
    assert conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 20
    assert conn.execute("SELECT count(*) FROM saved_searches").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM schema_version WHERE version=17").fetchone()[0] == 1


def test_active_comma_status_is_roundtrippable_state(search_client):
    assert validate_state({"q": "heart failure", "statuses": ["Active, not recruiting"]})["statuses"] == ["Active, not recruiting"]
    response = search_client.get("/api/trials/search", params={"q": "heart failure", "statuses": "Active%2C not recruiting"})
    assert response.json()["rules"]["statuses"] == ["Active, not recruiting"]
