"""Phase 3J project references, composite intelligence, and isolation."""
from datetime import datetime, timedelta, timezone

from db.connection import get_connection
from db.schema import create_schema
from tests.test_server_api import _insert_event

pytest_plugins = ("tests.test_trial_search",)


def _project(client, name="Myocarditis"):
    response = client.post("/api/projects", json={"name": name, "description": "Research topic"})
    assert response.status_code == 201, response.text
    return response.json()["project"]["id"]


def _recent_ts() -> str:
    """事件时间戳：UTC now-1h，稳落 briefing 7d 窗口。

    detected_at 生产约定是 SQLite datetime('now') = UTC（见 db/schema.py），
    overview 的时间窗也按 UTC 计算——这里必须用 UTC，本地钟（UTC+8）会
    被窗口上界排除；硬编码日期则会随 7 天窗口过期（时间炸弹）。
    """
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")


def _event(conn):
    rid = conn.execute("SELECT record_id FROM registry_records WHERE source_trial_id='NCT09000001' AND is_latest=1").fetchone()[0]
    _insert_event(conn, rid, "enrollment", "10", "20", "enrollment", _recent_ts())
    conn.commit()
    return conn.execute("SELECT max(event_id) FROM trial_events").fetchone()[0]


def test_v18_migration_is_idempotent_and_empty(test_db):
    conn = get_connection()
    create_schema(); create_schema()
    assert conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 20
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 0
    for table in ("project_saved_searches", "project_monitors", "project_trials", "project_notes", "project_evidence"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_project_crud_archive_and_associations_do_not_change_watch_or_monitor(search_client):
    conn = get_connection()
    pid = _project(search_client)
    other = _project(search_client, "CAR-T")
    sid = search_client.post("/api/saved-searches", json={"name": "My search", "state": {"q": "myocarditis"}}).json()["saved_search"]["id"]
    mid = conn.execute("INSERT INTO monitors(name) VALUES ('My monitor')").lastrowid
    conn.commit()
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("trial_watches", "monitor_events", "notifications")}
    for p in (pid, other):
        assert search_client.post(f"/api/projects/{p}/assets/searches", json={"id": sid}).status_code == 200
        assert search_client.post(f"/api/projects/{p}/assets/monitors", json={"id": mid}).status_code == 200
        assert search_client.post(f"/api/projects/{p}/assets/trials", json={"source": "NCT", "trial_id": "NCT09000001"}).status_code == 200
        assert search_client.post(f"/api/projects/{p}/assets/trials", json={"source": "NCT", "trial_id": "NCT09000001"}).json()["project"]["counts"]["trials"] == 1
    assert before == {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert search_client.patch(f"/api/projects/{pid}", json={"name": "Renamed", "pinned": True, "archived": True}).json()["project"]["archived_at"]
    assert conn.execute("SELECT enabled FROM monitors WHERE id=?", (mid,)).fetchone()[0] == 1
    assert len(search_client.get("/api/projects").json()["projects"]) == 1
    summaries = search_client.get("/api/projects?include_archived=true").json()["projects"]
    assert len(summaries) == 2
    assert summaries[0]["counts"]["trials"] == 1 and summaries[0]["active_monitors"] == 1
    assert "notes" not in summaries[0] and "evidence" not in summaries[0]
    assert search_client.patch(f"/api/projects/{pid}", json={"archived": False}).json()["project"]["archived_at"] is None
    assert search_client.delete(f"/api/projects/{pid}/assets/trials/NCT09000001?source=NCT").json()["project"]["counts"]["trials"] == 0
    assert search_client.get(f"/api/projects/{other}").json()["project"]["counts"]["trials"] == 1
    assert search_client.delete(f"/api/projects/{pid}").status_code == 200
    assert conn.execute("SELECT 1 FROM monitors WHERE id=?", (mid,)).fetchone()
    assert conn.execute("SELECT 1 FROM saved_searches WHERE id=?", (sid,)).fetchone()
    assert search_client.get(f"/api/projects/{other}").status_code == 200


def test_notes_evidence_and_exact_deep_link_identity(search_client):
    conn = get_connection(); eid = _event(conn); pid = _project(search_client)
    note = search_client.post(f"/api/projects/{pid}/notes", json={"body": "Review endpoint"}).json()["note"]
    assert search_client.patch(f"/api/projects/{pid}/notes/{note['id']}", json={"body": "Revisit later"}).json()["note"]["body"] == "Revisit later"
    assert search_client.post(f"/api/projects/{pid}/evidence", json={"event_id": eid, "note": "Key"}).status_code == 201
    assert search_client.post(f"/api/projects/{pid}/evidence", json={"event_id": eid, "note": "Updated"}).status_code == 201
    detail = search_client.get(f"/api/projects/{pid}").json()["project"]
    assert len(detail["evidence"]) == 1 and detail["evidence"][0]["event_id"] == eid
    assert detail["evidence"][0]["source"] == "NCT"
    exported = search_client.get(f"/api/projects/{pid}/export")
    assert exported.status_code == 200 and "text/markdown" in exported.headers["content-type"]
    assert f"event #{eid}" in exported.text and "Revisit later" in exported.text
    assert search_client.delete(f"/api/projects/{pid}/notes/{note['id']}").status_code == 200
    assert search_client.delete(f"/api/projects/{pid}/evidence/{eid}").status_code == 200
    assert search_client.get(f"/api/projects/{pid}").json()["project"]["counts"]["evidence"] == 0
    assert conn.execute("SELECT 1 FROM trial_events WHERE event_id=?", (eid,)).fetchone()


def test_project_updates_and_briefing_union_deduplicate(search_client):
    conn = get_connection(); eid = _event(conn); pid = _project(search_client)
    mids = [conn.execute("INSERT INTO monitors(name) VALUES (?)", (f"M{i}",)).lastrowid for i in range(2)]
    for mid in mids:
        conn.execute("INSERT INTO monitor_trials(monitor_id,trial_id,currently_matches) VALUES (?,'NCT09000001',1)", (mid,))
        conn.execute("INSERT INTO monitor_events(monitor_id,trial_id,event_type,related_change_event_id,detected_at) VALUES (?,'NCT09000001','trial_changed',?,?)", (mid, eid, _recent_ts()))
    conn.commit()
    for mid in mids:
        search_client.post(f"/api/projects/{pid}/assets/monitors", json={"id": mid})
    search_client.post(f"/api/projects/{pid}/assets/trials", json={"source": "NCT", "trial_id": "NCT09000001"})
    updates = search_client.get("/api/updates", params={"scope": "project", "project_id": pid, "window": "all"})
    assert updates.status_code == 200, updates.text
    assert updates.json()["total"] == 1
    assert updates.json()["items"][0]["event_id"] == eid
    assert updates.json()["items"][0]["project_trial"] is True
    assert updates.json()["items"][0]["project_monitors"] == ["M0", "M1"]
    matches = search_client.get(f"/api/projects/{pid}/monitor-trials")
    assert matches.status_code == 200
    assert matches.json()["total"] == 1
    assert matches.json()["trials"][0]["monitors"] == ["M0", "M1"]
    briefing = search_client.get("/api/intelligence/briefing", params={"scope": "project", "project_id": pid, "window": "7d"})
    assert briefing.status_code == 200, briefing.text
    assert briefing.json()["counts"]["change_events"] == 1
    assert briefing.json()["counts"]["changed_trials"] == 1
    assert search_client.get("/api/updates", params={"scope": "project"}).status_code == 422
    assert search_client.get("/api/intelligence/briefing", params={"scope": "project", "project_id": 999}).status_code == 404


def test_deleted_assets_cascade_project_links(search_client):
    conn = get_connection(); pid = _project(search_client)
    sid = search_client.post("/api/saved-searches", json={"name": "Search", "state": {"q": "myocarditis"}}).json()["saved_search"]["id"]
    mid = conn.execute("INSERT INTO monitors(name) VALUES ('Linked')").lastrowid; conn.commit()
    search_client.post(f"/api/projects/{pid}/assets/searches", json={"id": sid})
    search_client.post(f"/api/projects/{pid}/assets/monitors", json={"id": mid})
    search_client.delete(f"/api/saved-searches/{sid}")
    # Monitor API removes its owned rows; foreign-key cascade removes the project reference.
    search_client.delete(f"/api/monitors/{mid}")
    detail = search_client.get(f"/api/projects/{pid}").json()["project"]
    assert detail["counts"]["saved_searches"] == detail["counts"]["monitors"] == 0


def test_project_freshness_is_source_scoped_and_saved_search_is_passive(search_client):
    pid = _project(search_client)
    sid = search_client.post("/api/saved-searches", json={"name": "Only a query", "state": {"q": "myocarditis"}}).json()["saved_search"]["id"]
    search_client.post(f"/api/projects/{pid}/assets/searches", json={"id": sid})
    empty = search_client.get("/api/intelligence/briefing", params={"scope": "project", "project_id": pid}).json()
    assert empty["counts"]["change_events"] == 0
    assert empty["freshness"]["sources"] == []
    search_client.post(f"/api/projects/{pid}/assets/trials", json={"source": "NCT", "trial_id": "NCT09000001"})
    scoped = search_client.get("/api/intelligence/briefing", params={"scope": "project", "project_id": pid}).json()
    assert {s["short_name"] for s in scoped["freshness"]["sources"]} == {"NCT"}
