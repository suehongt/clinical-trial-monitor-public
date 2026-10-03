"""Explicit reconciliation of durable claims left by crashed workers."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from core.monitor_scheduler import next_occurrence


def reconcile_stale(conn: sqlite3.Connection, *, now: datetime | None = None,
                    older_than_minutes: int = 120) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=older_than_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    ingestion = conn.execute("""UPDATE registry_ingestion_runs
        SET status='interrupted',finished_at=?,failure_stage='interrupted',
            error_type='InterruptedRun',error_message='worker did not finalize run'
        WHERE status='running' AND started_at<?""", (stamp, cutoff)).rowcount
    rows = conn.execute("""SELECT mr.id,mr.monitor_id,m.schedule_frequency FROM monitor_runs mr
        JOIN monitors m ON m.id=mr.monitor_id
        WHERE mr.status='running' AND mr.started_at<?""", (cutoff,)).fetchall()
    for row in rows:
        conn.execute("UPDATE monitor_runs SET status='interrupted',completed_at=?,error_message='worker did not finalize run' WHERE id=?", (stamp, row["id"]))
        if row["schedule_frequency"] in {"hourly", "daily", "weekly"}:
            conn.execute("UPDATE monitors SET next_run_at=? WHERE id=? AND next_run_at<=?",
                         (next_occurrence(now, row["schedule_frequency"]), row["monitor_id"], stamp))
    email = conn.execute("""UPDATE notifications SET status='failed',claim_token=NULL,
        updated_at=? WHERE channel='email' AND status='sending' AND claimed_at<?""",
        (stamp, cutoff)).rowcount
    conn.execute("""UPDATE notification_delivery_attempts SET status='failed',finished_at=?,
        error_type='InterruptedDelivery',error_message='delivery result uncertain; review before retry'
        WHERE status='sending' AND notification_id IN
        (SELECT id FROM notifications WHERE channel='email' AND status='failed' AND updated_at=?)""",
        (stamp, stamp))
    conn.commit()
    return {"ingestion": ingestion, "monitor": len(rows), "email": email}
