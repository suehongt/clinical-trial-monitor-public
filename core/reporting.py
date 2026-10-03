"""
Reporting — generate daily / weekly summary reports.

Reports are based entirely on change_events, new registry_records,
and new master_trials since the last report of the same type.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from db.connection import get_connection, transaction
from db.schema import get_table_info

logger = logging.getLogger(__name__)


def generate_daily_report() -> Dict:
    """Generate today's daily report.  Returns summary dict."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _generate_report("daily", today)


def generate_weekly_report() -> Dict:
    """Generate the current weekly report (Mon-Sun)."""
    today = datetime.now(timezone.utc)
    # Find the most recent Monday
    monday = today - timedelta(days=today.weekday())
    week_label = monday.strftime("%Y-%m-%d")
    return _generate_report("weekly", week_label)


def _generate_report(report_type: str, period_start: str) -> Dict:
    """
    Create a report_status entry.  Idempotent — if a report already exists
    for this type + period_start, it is NOT regenerated (unless forced).
    """
    conn = get_connection()

    # Check if already generated
    existing = conn.execute(
        "SELECT report_id FROM report_status WHERE report_type = ? AND report_date = ?",
        (report_type, period_start),
    ).fetchone()
    if existing:
        logger.info("Report %s/%s already exists (id=%s), skipping",
                    report_type, period_start, existing["report_id"])
        return {"skipped": True, "report_id": existing["report_id"]}

    # Determine lookback window
    if report_type == "daily":
        lookback_hours = 48
    elif report_type == "weekly":
        lookback_hours = 168  # 7 days
    else:
        lookback_hours = 48

    # Count new records (exclude bootstrap imports)
    cur = conn.execute(
        """SELECT count(*) as cnt FROM registry_records
           WHERE first_crawled_at >= datetime('now', ?)
             AND is_bootstrap = 0""",
        (f"-{lookback_hours} hours",),
    )
    n_new = cur.fetchone()["cnt"]

    # Count new AGGREGATOR records (separate counter)
    cur = conn.execute(
        """SELECT count(*) as cnt FROM registry_records r
           JOIN registry_sources src ON src.source_id = r.source_id
           WHERE r.first_crawled_at >= datetime('now', ?)
             AND src.source_type = 'AGGREGATOR'""",
        (f"-{lookback_hours} hours",),
    )
    n_new_aggregator = cur.fetchone()["cnt"]

    # Count updated records (exclude bootstrap)
    cur = conn.execute(
        """SELECT count(*) as cnt FROM registry_records
           WHERE last_crawled_at >= datetime('now', ?)
             AND first_crawled_at != last_crawled_at
             AND is_bootstrap = 0""",
        (f"-{lookback_hours} hours",),
    )
    n_updated = cur.fetchone()["cnt"]

    # Count new master trials
    cur = conn.execute(
        """SELECT count(*) as cnt FROM master_trials
           WHERE created_at >= datetime('now', ?)""",
        (f"-{lookback_hours} hours",),
    )
    n_mt = cur.fetchone()["cnt"]

    # Count change events (from trial_events, the deduped event store)
    cur = conn.execute(
        """SELECT count(*) as cnt, sum(acknowledged) as ack
           FROM trial_events
           WHERE detected_at >= datetime('now', ?)""",
        (f"-{lookback_hours} hours",),
    )
    row = cur.fetchone()
    n_changes = row["cnt"] or 0
    n_ack = row["ack"] or 0

    # Build summary
    tables = get_table_info()
    summary = {
        "report_type": report_type,
        "period": period_start,
        "new_records": n_new,
        "updated_records": n_updated,
        "new_aggregator_records": n_new_aggregator,
        "new_master_trials": n_mt,
        "change_events": n_changes,
        "acknowledged": n_ack,
        "db_tables": tables,
        "sources": [
            dict(row) for row in
            conn.execute("SELECT short_name, full_name, enabled FROM registry_sources").fetchall()
        ],
    }

    with transaction():
        conn.execute("""
            INSERT INTO report_status
                (report_type, report_date, n_new_records, n_updated_records,
                 n_new_master_trials, n_change_events, n_acknowledged, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (report_type, period_start, n_new, n_updated, n_mt, n_changes, n_ack,
              json.dumps(summary, ensure_ascii=False)))

    logger.info("Generated %s report for %s (%d changes)", report_type, period_start, n_changes)
    return summary


def get_latest_report(report_type: str = "daily") -> Optional[Dict]:
    """Fetch the most recent report summary."""
    conn = get_connection()
    cur = conn.execute(
        """SELECT * FROM report_status WHERE report_type = ? ORDER BY report_date DESC LIMIT 1""",
        (report_type,),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    return None
