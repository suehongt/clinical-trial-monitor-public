from tests.test_server_api import seeded_client as _seeded_client
from tests.test_server_api import _insert_record

# Publish the shared fixture without an unused/redefined symbol in this test
# module; this keeps both direct-file and full-suite pytest collection valid.
globals()["seeded_client"] = _seeded_client


def test_monitor_schedule_can_be_enabled_changed_and_disabled(seeded_client):
    created = seeded_client.post(
        "/api/monitors", json={"name": "Scheduled topic", "rules": {}})
    monitor_id = created.json()["id"]

    hourly = seeded_client.patch(f"/api/monitors/{monitor_id}", json={
        "schedule_enabled": True, "schedule_frequency": "hourly",
        "schedule_timezone": "UTC",
    })
    assert hourly.status_code == 200
    hourly_monitor = hourly.json()["monitor"]
    assert hourly_monitor["schedule_enabled"] == 1
    assert hourly_monitor["schedule_frequency"] == "hourly"
    assert hourly_monitor["next_run_at"] is not None

    weekly = seeded_client.patch(f"/api/monitors/{monitor_id}", json={
        "schedule_enabled": True, "schedule_frequency": "weekly",
    })
    assert weekly.status_code == 200
    assert weekly.json()["monitor"]["schedule_frequency"] == "weekly"
    assert weekly.json()["monitor"]["next_run_at"] > hourly_monitor["next_run_at"]

    manual = seeded_client.patch(
        f"/api/monitors/{monitor_id}", json={"schedule_enabled": False})
    assert manual.status_code == 200
    assert manual.json()["monitor"]["schedule_enabled"] == 0
    assert manual.json()["monitor"]["next_run_at"] is None


def test_monitor_schedule_rejects_non_boolean_enabled(seeded_client):
    created = seeded_client.post(
        "/api/monitors", json={"name": "Invalid schedule", "rules": {}})
    monitor_id = created.json()["id"]
    response = seeded_client.patch(
        f"/api/monitors/{monitor_id}", json={"schedule_enabled": "yes"})
    assert response.status_code == 422


def test_monitor_next_run_can_be_pinned_and_is_validated(seeded_client):
    created = seeded_client.post(
        "/api/monitors", json={"name": "Pinned run", "rules": {}})
    monitor_id = created.json()["id"]
    seeded_client.patch(f"/api/monitors/{monitor_id}", json={
        "schedule_enabled": True, "schedule_frequency": "daily",
        "schedule_timezone": "UTC"})

    # manual pin (datetime-local shape from the UI) is normalized to the
    # scheduler's storage format instead of poisoning string comparisons
    pinned = seeded_client.patch(
        f"/api/monitors/{monitor_id}", json={"next_run_at": "2026-09-30T08:30"})
    assert pinned.status_code == 200
    assert pinned.json()["monitor"]["next_run_at"] == "2026-09-30 08:30:00"

    # re-reading the pin without a value keeps it (no silent recompute)
    assert seeded_client.get(f"/api/monitors/{monitor_id}").json()["monitor"]["next_run_at"] == "2026-09-30 08:30:00"

    bad = seeded_client.patch(
        f"/api/monitors/{monitor_id}", json={"next_run_at": "soon"})
    assert bad.status_code == 422
    bad_type = seeded_client.patch(
        f"/api/monitors/{monitor_id}", json={"next_run_at": 12345})
    assert bad_type.status_code == 422

def test_topic_monitor_baseline_preview_and_activity(seeded_client):
    rules={"query":"heart failure","registries":["NCT"],"statuses":["Recruiting"]}
    preview=seeded_client.post("/api/monitors/preview",json={"rules":rules})
    assert preview.status_code==200 and preview.json()["matched_count"] == 1
    created=seeded_client.post("/api/monitors",json={"name":"HF","rules":rules})
    assert created.status_code==200
    monitor_id=created.json()["id"]
    listed=seeded_client.get("/api/monitors").json()["monitors"]
    assert listed[0]["current_trials"] == 1
    # Creating a monitor establishes a quiet baseline: discovery/saving must
    # not manufacture an entered event or notification for known trials.
    activity=seeded_client.get(f"/api/monitors/{monitor_id}/activity").json()["activity"]
    assert activity == []
    rerun=seeded_client.post(f"/api/monitors/{monitor_id}/run").json()["summary"]
    assert rerun["new_count"] == 0


def test_search_rule_monitor_accepts_future_trial_and_leaves_when_rule_no_longer_matches(seeded_client):
    rules = {"query": "heart failure", "statuses": ["Recruiting"]}
    created = seeded_client.post("/api/monitors", json={"name": "HF search", "rules": rules}).json()
    monitor_id = created["id"]
    assert seeded_client.get(f"/api/monitors/{monitor_id}/activity").json()["activity"] == []

    from db.connection import get_connection
    conn = get_connection()
    _insert_record(conn, "NCT", "NCT00000077", "Future heart failure trial",
                   status="Recruiting", phase="Phase 3", conditions='["Heart Failure"]')
    conn.commit()
    entered = seeded_client.post(f"/api/monitors/{monitor_id}/run").json()["summary"]
    assert entered["new_count"] == 1

    completed_id = conn.execute("SELECT status_type_id FROM status_types WHERE label='Completed'").fetchone()[0]
    conn.execute("UPDATE registry_records SET status_id=? WHERE source_trial_id='NCT00000077'", (completed_id,))
    conn.commit()
    left = seeded_client.post(f"/api/monitors/{monitor_id}/run").json()["summary"]
    assert left["left_count"] == 1
