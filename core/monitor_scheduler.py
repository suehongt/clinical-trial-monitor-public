"""Deterministic one-shot scheduler for topic monitors (all timestamps UTC)."""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict
from core.monitors import run_monitor

INTERVALS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7)}
OCCURRENCE_FORMAT = "%Y-%m-%d %H:%M:%S"
def stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(OCCURRENCE_FORMAT)
def next_occurrence(now: datetime, frequency: str) -> str:
    """First slot of a freshly created/edited schedule: now + interval."""
    if frequency not in INTERVALS: raise ValueError("unsupported schedule frequency")
    return stamp(now + INTERVALS[frequency])

def advance_occurrence(occurrence: str, now: datetime, frequency: str) -> str:
    """Next slot after a claimed run, anchored to the stored occurrence.

    Rolling the anchor forward (instead of adding the interval to the tick
    clock) keeps the wall-clock time stable across days; skipping forward
    through slots missed during an outage avoids catch-up backlogs.
    """
    if frequency not in INTERVALS: raise ValueError("unsupported schedule frequency")
    due = datetime.strptime(occurrence, OCCURRENCE_FORMAT).replace(tzinfo=timezone.utc)
    while due <= now:
        due += INTERVALS[frequency]
    return stamp(due)

def run_due_monitors(conn: sqlite3.Connection, *, now: datetime) -> Dict[str, int]:
    """Claim each due UTC occurrence exactly once, then invoke canonical run_monitor.

    Missed schedules run once at their stored occurrence; the anchor then
    rolls forward through skipped slots (advance_occurrence), so the schedule
    keeps its wall-clock time without unbounded catch-up backlogs.
    """
    current = stamp(now); completed = failed = claimed = 0
    due = conn.execute("""SELECT id,schedule_frequency,next_run_at FROM monitors
        WHERE enabled=1 AND schedule_enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=?""", (current,)).fetchall()
    for monitor in due:
        occurrence = monitor["next_run_at"]
        # durable claim; uniqueness survives duplicate ticks, restarts, workers.
        cur = conn.execute("""INSERT OR IGNORE INTO monitor_runs (monitor_id,trigger,scheduled_for,status)
                            VALUES (?,'scheduled',?,'running')""", (monitor["id"], occurrence))
        conn.commit()
        if cur.rowcount != 1: continue
        claimed += 1
        try:
            run_monitor(conn, monitor["id"], trigger="scheduled", scheduled_for=occurrence, run_id=cur.lastrowid)
            completed += 1
        except Exception:
            failed += 1
        finally:
            try:
                nxt = advance_occurrence(occurrence, now, monitor["schedule_frequency"])
            except ValueError:
                nxt = next_occurrence(now, monitor["schedule_frequency"])
            conn.execute("UPDATE monitors SET next_run_at=? WHERE id=?", (nxt, monitor["id"])); conn.commit()
    return {"due": len(due), "claimed": claimed, "completed": completed, "failed": failed}
