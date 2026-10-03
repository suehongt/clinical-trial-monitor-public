"""Release gate checks use disposable databases only."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.integrity import audit, open_readonly, startup_check
from db.backup import backup_database
from db.connection import get_connection
from db.recovery import restore_database, verify_database
from core.recovery import reconcile_stale
from core.redaction import redact
from server.app import create_app

pytest_plugins = ("tests.test_trial_search",)


def test_clean_integrity_and_foreign_keys(test_db):
    with closing(open_readonly(test_db)) as conn:
        assert startup_check(conn)["schema_version"] == 20
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert audit(conn)["status"] == "ok"
    assert get_connection().execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_orphan_and_stale_claim_are_reported_without_repair(test_db):
    with sqlite3.connect(str(test_db)) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO notifications(monitor_event_id,event_type,channel) VALUES (999,'trial_changed','in_app')")
        conn.execute("INSERT INTO monitor_runs(monitor_id,started_at,status) VALUES (999,'2000-01-01','running')")
    with closing(open_readonly(test_db)) as conn:
        result = audit(conn)
    codes = {item["code"] for item in result["issues"]}
    assert {"FOREIGN_KEY", "NOTIFICATION_EVENT_MISSING", "STALE_MONITOR_RUN"} <= codes
    with sqlite3.connect(str(test_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_duplicate_logical_event_detected_despite_different_hashes(test_db):
    conn = get_connection()
    source = conn.execute("SELECT source_id FROM registry_sources LIMIT 1").fetchone()[0]
    record = conn.execute("INSERT INTO registry_records(source_id,source_trial_id) VALUES (?,'NCT99999999')", (source,)).lastrowid
    conn.executemany("""INSERT INTO trial_events(record_id,field_name,old_value,new_value,event_hash,detected_at)
        VALUES (?,'enrollment','10','20',?,'2026-09-22 00:00:00')""",
        [(record, "hash-a"), (record, "hash-b")])
    conn.commit()
    with closing(open_readonly(test_db)) as read_conn:
        issues = audit(read_conn)["issues"]
    assert any(issue["code"] == "DUPLICATE_LOGICAL_EVENT" for issue in issues)


def test_backup_restore_recovers_project_monitor_and_note(test_db, tmp_path):
    conn = get_connection()
    project_id = conn.execute("INSERT INTO research_projects(name) VALUES ('Restore me')").lastrowid
    monitor_id = conn.execute("INSERT INTO monitors(name) VALUES ('Test monitor')").lastrowid
    conn.execute("INSERT INTO project_monitors(project_id,monitor_id) VALUES (?,?)", (project_id, monitor_id))
    conn.execute("INSERT INTO project_notes(project_id,body) VALUES (?,?)", (project_id, "Evidence note"))
    conn.commit()
    backup = backup_database(str(test_db), str(tmp_path / "backups"))
    assert verify_database(backup)["schema_version"] == 20
    conn.execute("DELETE FROM project_notes")
    conn.execute("DELETE FROM project_monitors")
    conn.execute("DELETE FROM research_projects")
    conn.commit()
    from db.connection import close_connection
    close_connection()
    restored = restore_database(backup, test_db, offline=True)
    assert restored == test_db.resolve()
    with sqlite3.connect(str(test_db)) as recovered:
        assert recovered.execute("SELECT body FROM project_notes").fetchone()[0] == "Evidence note"
        assert recovered.execute("SELECT COUNT(*) FROM project_monitors").fetchone()[0] == 1


def test_corrupt_restore_refuses_without_touching_target(test_db, tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    before = test_db.stat().st_size
    with pytest.raises(sqlite3.DatabaseError):
        restore_database(corrupt, test_db, offline=True)
    assert test_db.stat().st_size == before


def test_restore_refuses_active_writer(test_db, tmp_path):
    backup = backup_database(str(test_db), str(tmp_path / "backups"))
    writer = get_connection()
    writer.execute("INSERT INTO research_projects(name) VALUES ('Uncommitted writer')")
    try:
        with pytest.raises(sqlite3.OperationalError):
            restore_database(backup, test_db, offline=True)
    finally:
        writer.rollback()


def test_future_schema_refused_before_migration(test_db):
    from db.schema import create_schema
    conn = get_connection()
    conn.execute("INSERT INTO schema_version(version,description) VALUES (99,'future')")
    conn.commit()
    with pytest.raises(RuntimeError, match="unsupported schema"):
        create_schema()
    with pytest.raises(RuntimeError, match="unsupported database schema"):
        with closing(open_readonly(test_db)) as read_conn:
            startup_check(read_conn)
    with pytest.raises(RuntimeError, match="unsupported database schema"):
        create_app(str(test_db))


def test_interrupted_v19_migration_keeps_version_until_retry(test_db, monkeypatch):
    import db.schema as schema
    conn = get_connection()
    # 模拟 v19 迁移被打断：19、20 都未落账（v20 建立在 v19 之上）。
    # 只删 19 时最新版本行仍是 20，迁移步骤整段跳过，坏 DDL 永不执行。
    conn.execute("DELETE FROM schema_version WHERE version IN (19, 20)")
    conn.commit()
    real_ddl = schema.SQL_CREATE_GLOSSARY_TERMS
    monkeypatch.setattr(schema, "SQL_CREATE_GLOSSARY_TERMS",
                        "CREATE TABLE migration_partial(id INTEGER);\nCREATE TABLE broken (")
    with pytest.raises(sqlite3.DatabaseError):
        schema.create_schema()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 18
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='migration_partial'").fetchone() is None
    monkeypatch.setattr(schema, "SQL_CREATE_GLOSSARY_TERMS", real_ddl)
    schema.create_schema()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 20


def test_production_headers_request_id_and_limits(test_db, monkeypatch):
    monkeypatch.setenv("CT_MODE", "production")
    with TestClient(create_app(str(test_db))) as client:
        ready = client.get("/api/ready")
        assert ready.status_code == 200
        assert ready.json()["schema_version"] == 20
        assert ready.headers["x-frame-options"] == "DENY"
        assert ready.headers["x-request-id"]
        assert client.get("/api/docs").status_code == 404
        big = client.post("/api/projects", content=b"x" * 65537)
        assert big.status_code == 413
        streamed = client.post("/api/projects", content=(b"x" * 32768 for _ in range(3)),
                               headers={"Transfer-Encoding": "chunked"})
        assert streamed.status_code == 413
        assert streamed.json()["error"]["code"] == "REQUEST_TOO_LARGE"
        bad = client.post("/api/projects", json={"name": ""})
        assert bad.status_code == 422
        assert bad.json()["error"]["request_id"] == bad.headers["x-request-id"]
        forbidden = client.post("/api/projects", json={"name": "Cross-origin"},
                                headers={"Origin": "https://evil.example"})
        assert forbidden.status_code == 403


def test_production_rejects_invalid_mode_and_partial_smtp(test_db, monkeypatch):
    monkeypatch.setenv("CT_MODE", "invalid")
    with pytest.raises(RuntimeError, match="CT_MODE"):
        create_app(str(test_db))
    monkeypatch.setenv("CT_MODE", "production")
    monkeypatch.setenv("CT_EMAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.delenv("CT_EMAIL_FROM", raising=False)
    with pytest.raises(RuntimeError, match="incomplete SMTP"):
        create_app(str(test_db))


def test_release_flow_search_monitor_project_and_recovery(search_client, test_db, tmp_path):
    """One disposable DB carries the full operator workflow through restore."""
    from tests.test_server_api import _insert_event

    client = search_client
    assert client.get("/api/ready").status_code == 200
    search = client.get("/api/trials/search", params={"q": "myocarditis"})
    assert search.status_code == 200
    assert any(t["id"] == "NCT09000001" for t in search.json()["trials"])
    saved = client.post("/api/saved-searches", json={"name": "Myocarditis", "state": {"q": "myocarditis"}})
    assert saved.status_code == 201
    saved_id = saved.json()["saved_search"]["id"]
    monitor = client.post("/api/monitors", json={"name": "Myocarditis changes", "rules": {"query": "myocarditis"}})
    assert monitor.status_code == 200
    monitor_id = monitor.json()["id"]
    project = client.post("/api/projects", json={"name": "Myocarditis review"})
    assert project.status_code == 201
    project_id = project.json()["project"]["id"]
    for kind, asset_id in (("searches", saved_id), ("monitors", monitor_id)):
        assert client.post(f"/api/projects/{project_id}/assets/{kind}", json={"id": asset_id}).status_code == 200

    # A deterministic registry-change fixture arrives after the monitor's
    # quiet baseline; the real monitor and notification services process it.
    conn = get_connection()
    record_id = conn.execute("SELECT record_id FROM registry_records WHERE source_trial_id='NCT09000001'").fetchone()[0]
    conn.execute("UPDATE monitor_runs SET completed_at=datetime('now','-1 minute') WHERE monitor_id=?", (monitor_id,))
    conn.commit()
    class ChangedSourceFixture:
        source_id = conn.execute("SELECT source_id FROM registry_sources WHERE short_name='NCT'").fetchone()[0]

        def run(self, *, since=None, is_bootstrap=False):
            detected = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE registry_records SET enrollment=61 WHERE record_id=?", (record_id,))
            _insert_event(conn, record_id, "enrollment", "60", "61", "enrollment", detected)
            conn.commit()
            return {"status": "succeeded", "found": 1, "new": 0, "updated": 1,
                    "skipped": 0, "failed": 0, "changes": 1}

    from core.ingestion import run_registry_ingestion
    ingestion = run_registry_ingestion("clinicaltrials_gov", collector=ChangedSourceFixture())
    assert ingestion["status"] == "succeeded" and ingestion["changes"] == 1
    event_id = conn.execute("SELECT MAX(event_id) FROM trial_events").fetchone()[0]
    run = client.post(f"/api/monitors/{monitor_id}/run")
    assert run.status_code == 200 and run.json()["summary"]["changed_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE monitor_id=?", (monitor_id,)).fetchone()[0] >= 1
    briefing = client.get("/api/intelligence/briefing", params={"scope": "project", "project_id": project_id})
    assert briefing.status_code == 200 and briefing.json()["counts"]["change_events"] >= 1
    assert client.post(f"/api/projects/{project_id}/notes", json={"body": "Review enrolment"}).status_code == 201
    assert client.post(f"/api/projects/{project_id}/evidence", json={"event_id": event_id}).status_code == 201

    backup = backup_database(test_db, tmp_path / "backups")
    restored_path = tmp_path / "restored.db"
    restore_database(backup, restored_path, offline=True)
    with closing(open_readonly(restored_path)) as restored:
        assert audit(restored)["status"] == "ok"
        assert restored.execute("SELECT COUNT(*) FROM project_notes WHERE project_id=?", (project_id,)).fetchone()[0] == 1
        assert restored.execute("SELECT COUNT(*) FROM project_evidence WHERE project_id=?", (project_id,)).fetchone()[0] == 1


def test_production_unhandled_error_is_sanitized(test_db, monkeypatch):
    monkeypatch.setenv("CT_MODE", "production")
    app = create_app(str(test_db))

    @app.get("/api/failure-fixture")
    def fail():
        raise RuntimeError("password=should-never-appear")

    # The production app mounts the SPA last; insert this test-only route
    # before the catch-all static mount.
    app.router.routes.insert(0, app.router.routes.pop())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/failure-fixture")
        assert response.status_code == 500
        assert "should-never-appear" not in response.text
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_stale_worker_claims_reconcile_without_replay(test_db):
    conn = get_connection()
    source_id = conn.execute("SELECT source_id FROM registry_sources LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO registry_ingestion_runs(source_id,status,started_at) VALUES (?,'running','2000-01-01')", (source_id,))
    mid = conn.execute("INSERT INTO monitors(name,schedule_enabled,schedule_frequency,next_run_at) VALUES ('stale',1,'daily','2000-01-01')").lastrowid
    conn.execute("INSERT INTO monitor_runs(monitor_id,status,started_at,scheduled_for) VALUES (?,'running','2000-01-01','2000-01-01')", (mid,))
    conn.execute("INSERT INTO notifications(monitor_event_id,event_type,channel,status,claimed_at) VALUES (999,'trial_changed','email','sending','2000-01-01')")
    conn.commit()
    result = reconcile_stale(conn, now=datetime(2026, 9, 22, tzinfo=timezone.utc))
    assert result == {"ingestion": 1, "monitor": 1, "email": 1}
    assert reconcile_stale(conn, now=datetime(2026, 9, 22, tzinfo=timezone.utc)) == {"ingestion": 0, "monitor": 0, "email": 0}
    assert conn.execute("SELECT status FROM monitor_runs WHERE monitor_id=?", (mid,)).fetchone()[0] == "interrupted"


def test_secret_redaction(monkeypatch):
    monkeypatch.setenv("CT_EMAIL_SMTP_PASSWORD", "secret-value-123")
    assert "secret-value-123" not in redact("password=secret-value-123 and token=abc")


def test_disabled_smtp_keeps_pending_delivery(test_db, monkeypatch):
    from core.notifications import deliver_pending_email_notifications
    for name in ("CT_EMAIL_SMTP_HOST", "CT_EMAIL_FROM", "CT_EMAIL_SMTP_USER", "CT_EMAIL_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    conn = get_connection()
    monitor = conn.execute("INSERT INTO monitors(name) VALUES ('mail')").lastrowid
    event = conn.execute("INSERT INTO monitor_events(monitor_id,trial_id,event_type) VALUES (?,'NCT1','trial_left')", (monitor,)).lastrowid
    notification = conn.execute("""INSERT INTO notifications(monitor_event_id,event_type,channel,status,recipient)
        VALUES (?,'trial_left','email','pending','test@example.org')""", (event,)).lastrowid
    conn.commit()
    assert deliver_pending_email_notifications(conn) == {"delivered": 0, "failed": 0, "disabled": 1}
    assert conn.execute("SELECT status FROM notifications WHERE id=?", (notification,)).fetchone()[0] == "pending"


def test_large_project_trials_have_stable_bounded_pages(test_db):
    conn = get_connection()
    pid = conn.execute("INSERT INTO research_projects(name) VALUES ('Large')").lastrowid
    conn.executemany("INSERT INTO project_trials(project_id,source,trial_id) VALUES (?,'NCT',?)",
                     [(pid, f"NCT{i:08d}") for i in range(150)])
    conn.commit()
    with TestClient(create_app(str(test_db))) as client:
        first = client.get(f"/api/projects/{pid}?trial_page=1").json()["project"]
        second = client.get(f"/api/projects/{pid}?trial_page=2").json()["project"]
        assert first["counts"]["trials"] == 150
        assert len(first["trials"]) == 100
        assert len(second["trials"]) == 50
        assert {row["trial_id"] for row in first["trials"]}.isdisjoint(
            {row["trial_id"] for row in second["trials"]})
        assert client.get(f"/api/projects/{pid}?page_size=1000").status_code == 422


def test_monitor_text_and_rule_limits(test_db):
    with TestClient(create_app(str(test_db))) as client:
        assert client.post("/api/monitors", json={"name": "x" * 121, "rules": {}}).status_code == 422
        assert client.post("/api/monitors/preview", json={"rules": {"query": "x" * 501}}).status_code == 422
        assert client.post("/api/monitors/preview", json={"rules": {"country": ["x"] * 21}}).status_code == 422
        assert client.get("/api/trials/search", params={"q": "x" * 501}).status_code == 422
