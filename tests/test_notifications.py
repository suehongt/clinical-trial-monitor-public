def test_notification_outbox_is_post_cutover_idempotent_and_skips_baseline(test_db):
    from db.connection import get_connection
    from core.notifications import reconcile_notifications
    conn = get_connection()
    monitor_id = conn.execute("INSERT INTO monitors (name) VALUES ('m')").lastrowid
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,metadata) VALUES (?,?,'trial_entered','{\"initial_population\": true}')", (monitor_id, "A"))
    changed = conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_changed')", (monitor_id, "A")).lastrowid
    conn.commit()
    assert reconcile_notifications(conn)["created"] == 1
    assert reconcile_notifications(conn)["created"] == 0
    row = conn.execute("SELECT monitor_event_id,status,read_at FROM notifications").fetchone()
    assert row["monitor_event_id"] == changed and row["status"] == "delivered" and row["read_at"] is None
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,detected_at) VALUES (?,?,'trial_left','2000-01-01 00:00:00')", (monitor_id, "A"))
    conn.commit()
    assert reconcile_notifications(conn)["created"] == 0


def test_failed_notification_retries_same_durable_identity(test_db):
    from db.connection import get_connection
    from core.notifications import reconcile_notifications, retry_failed_notification
    conn = get_connection()
    monitor_id = conn.execute("INSERT INTO monitors (name) VALUES ('m')").lastrowid
    event_id = conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_left')", (monitor_id, "A")).lastrowid
    conn.commit()
    reconcile_notifications(conn)
    notification_id = conn.execute("SELECT id FROM notifications WHERE monitor_event_id=?", (event_id,)).fetchone()[0]
    conn.execute("UPDATE notifications SET status='failed' WHERE id=?", (notification_id,)); conn.commit()
    assert retry_failed_notification(conn, notification_id) == {"delivered": 1, "failed": 0}
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE monitor_event_id=?", (event_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notification_delivery_attempts WHERE notification_id=?", (notification_id,)).fetchone()[0] == 2


def test_notification_api_read_state_and_unread_count(test_db):
    from db.connection import get_connection
    from core.notifications import reconcile_notifications
    from fastapi.testclient import TestClient
    from server.app import create_app
    client = TestClient(create_app(str(test_db)))
    conn = get_connection()
    monitor_id = conn.execute("INSERT INTO monitors (name) VALUES ('API monitor')").lastrowid
    first = conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_entered')", (monitor_id, "NCT00000001")).lastrowid
    second = conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_left')", (monitor_id, "NCT00000002")).lastrowid
    conn.commit(); reconcile_notifications(conn)
    notes = client.get("/api/notifications").json()["notifications"]
    assert {n["monitor_event_id"] for n in notes} >= {first, second}
    assert all("trial_title" in n and "monitor_name" in n for n in notes)
    assert client.get("/api/notifications/unread-count").json()["count"] == 2
    one = next(n for n in notes if n["monitor_event_id"] == first)
    assert client.patch(f"/api/notifications/{one['id']}", json={"read": True}).status_code == 200
    assert client.get("/api/notifications?unread=true").json()["notifications"][0]["monitor_event_id"] == second
    assert client.get("/api/notifications/unread-count").json()["count"] == 1
    assert client.post("/api/notifications/mark-all-read").json()["updated"] == 1
    assert client.get("/api/notifications/unread-count").json()["count"] == 0
    assert client.patch("/api/notifications/999999", json={"read": True}).status_code == 404
