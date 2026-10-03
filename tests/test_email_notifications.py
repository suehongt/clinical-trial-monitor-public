from dataclasses import dataclass


@dataclass
class FakeProvider:
    failures: set[str]
    name: str = "fake"
    sent: list[str] = None  # type: ignore[assignment]
    def __post_init__(self): self.sent = []
    def send_email(self, *, to, subject, text_body, idempotency_key, **_):
        self.sent.append(to)
        if to in self.failures: raise RuntimeError("planned provider failure")
        from core.email_provider import EmailResult
        return EmailResult("fake", f"message-{len(self.sent)}")


def test_email_preference_cutover_failure_isolation_and_retry(test_db):
    from db.connection import get_connection
    from core.notifications import reconcile_notifications, deliver_pending_email_notifications, retry_failed_notification
    conn = get_connection()
    first = conn.execute("INSERT INTO monitors (name,email_notifications_enabled,email_recipient,email_enabled_at) VALUES ('first',1,'fail@example.test',datetime('now'))").lastrowid
    second = conn.execute("INSERT INTO monitors (name,email_notifications_enabled,email_recipient,email_enabled_at) VALUES ('second',1,'ok@example.test',datetime('now'))").lastrowid
    # Existing event is pre-preference and therefore must never be backfilled.
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,detected_at) VALUES (?,?,'trial_changed','2000-01-01 00:00:00')", (first, "OLD"))
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_entered')", (first, "A"))
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_left')", (second, "B")); conn.commit()
    result = reconcile_notifications(conn)
    assert result["email_created"] == 2
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE channel='email'").fetchone()[0] == 2
    provider = FakeProvider({"fail@example.test"})
    assert deliver_pending_email_notifications(conn, provider) == {"delivered": 1, "failed": 1}
    failed = conn.execute("SELECT id,recipient FROM notifications WHERE channel='email' AND status='failed'").fetchone()
    assert failed["recipient"] == "fail@example.test"
    # Recipient changes affect future notifications only; retry preserves snapshot.
    conn.execute("UPDATE monitors SET email_recipient='new@example.test' WHERE id=?", (first,)); conn.commit()
    assert retry_failed_notification(conn, failed["id"], FakeProvider(set())) == {"delivered": 1, "failed": 0}
    assert conn.execute("SELECT recipient FROM notifications WHERE id=?", (failed["id"],)).fetchone()[0] == "fail@example.test"
    assert conn.execute("SELECT COUNT(*) FROM notification_delivery_attempts WHERE notification_id=?", (failed["id"],)).fetchone()[0] == 2


def test_email_requires_enabled_preference_and_valid_recipient(test_db):
    from db.connection import get_connection
    from core.notifications import reconcile_notifications
    conn = get_connection()
    mid = conn.execute("INSERT INTO monitors (name,email_notifications_enabled,email_recipient) VALUES ('m',0,'x@example.test')").lastrowid
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_left')", (mid, "A")); conn.commit()
    assert reconcile_notifications(conn)["email_created"] == 0
    conn.execute("UPDATE monitors SET email_notifications_enabled=1,email_enabled_at=datetime('now'),email_recipient='not-an-email' WHERE id=?", (mid,))
    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_reentered')", (mid, "A")); conn.commit()
    assert reconcile_notifications(conn)["email_created"] == 0
