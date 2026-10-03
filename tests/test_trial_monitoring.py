"""Phase-2 individual watch API contracts over the deterministic API fixture."""
from __future__ import annotations

from tests.test_server_api import seeded_client as _seeded_client

globals()["seeded_client"] = _seeded_client

def test_watch_unwatch_rewatch_and_defaults(seeded_client):
    url = "/api/trials/NCT/NCT00000001/watch"
    first = seeded_client.post(url)
    assert first.status_code == 200
    watch = first.json()["watch"]
    assert watch["enabled"] is True
    assert {p["field_group"] for p in watch["preferences"]} >= {"status", "primary_outcomes", "sponsor"}
    # idempotent: same persisted row, no duplicate.
    assert seeded_client.post(url).json()["watch"]["id"] == watch["id"]
    assert seeded_client.delete(url).json()["watch"]["enabled"] is False
    assert seeded_client.post(url).json()["watch"]["enabled"] is True


def test_invalid_trial_cannot_be_watched(seeded_client):
    assert seeded_client.post("/api/trials/NCT/NCT99999999/watch").status_code == 404


def test_watched_summary_preferences_events_timeline_and_versions(seeded_client):
    watch = seeded_client.post("/api/trials/NCT/NCT00000001/watch").json()["watch"]
    listed = seeded_client.get("/api/watches").json()
    assert listed["total"] == 1
    assert listed["watches"][0]["unseen_change_count"] == 3
    changed = seeded_client.patch(f"/api/watches/{watch['id']}/preferences", json=[{
        "field_group": "contacts", "enabled": True, "minimum_severity": "important",
    }])
    assert changed.status_code == 200
    events = seeded_client.get("/api/trials/NCT/NCT00000001/changes", params={"limit": 2}).json()
    assert events["total"] == 3 and len(events["events"]) == 2
    assert {"field_group", "from_version", "to_version", "severity"} <= set(events["events"][0])
    timeline = seeded_client.get("/api/trials/NCT/NCT00000001/timeline").json()["timeline"]
    # Two current-version field events comprise one grouped registry update.
    assert any(len(group["events"]) == 2 for group in timeline)
    versions = seeded_client.get("/api/trials/NCT/NCT00000001/versions").json()["versions"]
    assert versions and {"id", "version_number", "change_count", "highest_change_severity"} <= set(versions[0])
