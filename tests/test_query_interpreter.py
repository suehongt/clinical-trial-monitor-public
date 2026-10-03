"""Phase 3H: interpretation proposes existing deterministic rule fields only."""
import json

import pytest

from core.query_interpreter import interpret, validate_proposal
pytest_plugins = ("tests.test_trial_search",)


@pytest.mark.parametrize("text, expected", [
    ("Recruiting phase 2 or 3 myocarditis trials in China",
     {"query": ["myocarditis"], "country": ["China"], "statuses": ["Recruiting"], "phase": ["Phase 2", "Phase 3"]}),
    ("中国正在招募的II期心肌炎临床试验",
     {"query": ["心肌炎"], "country": ["China"], "statuses": ["Recruiting"], "phase": ["Phase 2"]}),
    ("ChiCTR 心肌炎试验", {"query": ["心肌炎"], "registries": ["ChiCTR"]}),
    ("Phase 3 heart failure studies sponsored by Novartis",
     {"query": ["heart failure"], "phase": ["Phase 3"], "sponsor": ["Novartis"]}),
    ("Observational troponin studies in the United States",
     {"query": ["troponin"], "study_types": ["Observational"], "country": ["United States"]}),
    ("intervention: CAR-T trials", {"intervention": ["CAR-T"]}),
    ("condition: myocarditis trials", {"condition": ["myocarditis"]}),
    ("ClinicalTrials.gov CAR-T myocarditis trials", {"query": ["CAR-T myocarditis"], "registries": ["NCT"]}),
    ("EudraCT myocarditis trials", {"query": ["myocarditis"], "registries": ["EUCTR"]}),
])
def test_supported_interpretations(text, expected):
    assert interpret(text)["rule"] == expected


def test_country_and_registry_never_conflated():
    assert interpret("myocarditis trials in China")["rule"].get("registries") is None
    assert interpret("ChiCTR myocarditis trials")["rule"].get("country") is None


@pytest.mark.parametrize("text", ["Recruiting phase 2 myocarditis trials in China",
                                    "中国正在招募的II期心肌炎临床试验"])
def test_bilingual_constraint_equivalence(text):
    rule = interpret(text)["rule"]
    assert rule["country"] == ["China"]
    assert rule["statuses"] == ["Recruiting"]
    assert rule["phase"] == ["Phase 2"]
    assert rule["query"] in (["myocarditis"], ["心肌炎"])


def test_unsupported_and_ambiguous_are_visible_not_executable():
    recent = interpret("recent myocarditis trials")
    assert recent["ambiguities"] and recent["rule"] == {"query": ["myocarditis"]}
    promising = interpret("most promising myocarditis trials")
    assert promising["unsupported"] and promising["rule"] == {"query": ["myocarditis"]}
    dated = interpret("myocarditis trials updated in the last 6 months")
    assert dated["unsupported"] and set(dated["rule"]) == {"query"}
    assert interpret("active myocarditis trials")["ambiguities"]
    assert interpret("Chinese registry myocarditis trials")["ambiguities"]
    assert interpret("Observational troponin studies in Europe")["unsupported"]


def test_chinese_long_query_does_not_drop_unexecutable_date():
    result = interpret("过去半年中国开展的、仍在招募的 II/III 期心肌炎药物干预试验")
    assert result["rule"] == {"query": ["心肌炎"], "country": ["China"],
                              "statuses": ["Recruiting"], "study_types": ["Interventional"],
                              "phase": ["Phase 2", "Phase 3"]}
    assert result["unsupported"]


@pytest.mark.parametrize("bad", [{"registries": ["Fake"]}, {"statuses": ["Active"]},
                                  {"phase": ["Phase 8"]}, {"updated_since": ["today"]},
                                  {"query": "myocarditis"}, {"country": [42]}])
def test_strict_rule_validation(bad):
    with pytest.raises(ValueError):
        validate_proposal(bad)


def test_api_rejects_malformed_and_has_no_side_effects(search_client):
    client = search_client
    for payload in [{}, {"text": ""}, {"text": 10}, {"text": "a" * 501},
                    {"text": "myocarditis", "rules": {"statuses": ["Completed"]}}]:
        assert client.post("/api/search/interpret", json=payload).status_code == 422
    from db.connection import get_connection
    conn = get_connection()
    before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
              for table in ("monitors", "monitor_trials", "monitor_events", "notifications")}
    response = client.post("/api/search/interpret", json={"text": "Recruiting myocarditis trials in China"})
    assert response.status_code == 200
    assert response.json()["rule"] == {"query": ["myocarditis"], "statuses": ["Recruiting"], "country": ["China"]}
    assert before == {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}


def test_interpretation_search_and_monitor_equivalence_and_stability(search_client, monkeypatch):
    client = search_client
    rule = client.post("/api/search/interpret", json={"text": "Recruiting myocarditis trials in China"}).json()["rule"]
    natural = client.get("/api/trials/search", params={"q": rule["query"][0],
                                                      "statuses": rule["statuses"][0],
                                                      "country": rule["country"][0]}).json()
    structured = client.get("/api/trials/search", params={"q": "myocarditis", "statuses": "Recruiting", "country": "China"}).json()
    assert natural["rules"] == structured["rules"]
    assert {t["id"] for t in natural["trials"]} == {t["id"] for t in structured["trials"]}
    saved = client.post("/api/monitors", json={"name": "NL trial search", "rules": rule})
    assert saved.status_code == 200
    from db.connection import get_connection
    from core.monitors import matching_trial_ids, run_monitor
    conn = get_connection()
    monitor_id = saved.json()["id"]
    assert json.loads(conn.execute("SELECT rules_json FROM monitor_rules WHERE monitor_id=?", (monitor_id,)).fetchone()[0]) == rule
    before = matching_trial_ids(conn, rule)
    monkeypatch.setattr("core.query_interpreter.interpret", lambda text: {"rule": {"query": ["heart failure"]}})
    run_monitor(conn, monitor_id)
    assert matching_trial_ids(conn, rule) == before
    assert conn.execute("SELECT matched_count FROM monitor_runs WHERE monitor_id=? ORDER BY id DESC LIMIT 1", (monitor_id,)).fetchone()[0] == len(before)
