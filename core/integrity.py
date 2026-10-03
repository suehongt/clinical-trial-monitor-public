"""Read-only, deterministic checks for the supported SQLite v20 database."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = 20
REQUIRED_TABLES = frozenset({
    "schema_version", "registry_sources", "registry_records", "trial_events",
    "registry_ingestion_runs", "sync_status", "monitors", "monitor_runs",
    "monitor_events", "notifications", "notification_delivery_attempts",
    "saved_searches", "research_projects", "project_saved_searches",
    "project_monitors", "project_trials", "project_notes", "project_evidence",
    "glossary_terms",
})


def open_readonly(path: str | Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    conn = sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def startup_check(conn: sqlite3.Connection) -> dict:
    """Cheap gate: never creates or migrates a database."""
    conn.execute("SELECT 1").fetchone()
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(REQUIRED_TABLES - tables)
    if missing:
        raise RuntimeError("missing required database tables: " + ", ".join(missing))
    row = conn.execute("SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1").fetchone()
    version = row[0] if row else None
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported database schema {version}; expected {SCHEMA_VERSION}")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign-key enforcement is disabled")
    return {"schema_version": version, "database": "ok"}


def audit(conn: sqlite3.Connection, *, now: datetime | None = None,
          stale_minutes: int = 120) -> dict:
    """Inspect persisted state without writing; findings have stable codes."""
    issues: list[dict] = []

    def add(severity: str, code: str, detail: str, count: int = 1) -> None:
        issues.append({"severity": severity, "code": code, "detail": detail, "count": count})

    try:
        startup_check(conn)
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        add("critical", "SCHEMA_UNSAFE", str(exc))
        return _result(issues)

    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        add("critical", "SQLITE_CORRUPT", str(check))
    for row in conn.execute("PRAGMA foreign_key_check"):
        add("critical", "FOREIGN_KEY", f"{row[0]} row {row[1]} references {row[2]}")

    checks = (
        ("critical", "NOTIFICATION_EVENT_MISSING", "SELECT COUNT(*) FROM notifications n LEFT JOIN monitor_events e ON e.id=n.monitor_event_id WHERE e.id IS NULL"),
        ("error", "MONITOR_EVENT_MONITOR_MISSING", "SELECT COUNT(*) FROM monitor_events e LEFT JOIN monitors m ON m.id=e.monitor_id WHERE m.id IS NULL"),
        ("error", "MONITOR_EVENT_TRIAL_EVENT_MISSING", "SELECT COUNT(*) FROM monitor_events e LEFT JOIN trial_events t ON t.event_id=e.related_change_event_id WHERE e.related_change_event_id IS NOT NULL AND t.event_id IS NULL"),
        ("error", "EVIDENCE_EVENT_MISSING", "SELECT COUNT(*) FROM project_evidence p LEFT JOIN trial_events t ON t.event_id=p.event_id WHERE t.event_id IS NULL"),
        ("error", "PROJECT_SEARCH_MISSING", "SELECT COUNT(*) FROM project_saved_searches p LEFT JOIN saved_searches s ON s.id=p.saved_search_id WHERE s.id IS NULL"),
        ("error", "PROJECT_MONITOR_MISSING", "SELECT COUNT(*) FROM project_monitors p LEFT JOIN monitors m ON m.id=p.monitor_id WHERE m.id IS NULL"),
        ("error", "PROJECT_TRIAL_MISSING", "SELECT COUNT(*) FROM project_trials p WHERE NOT EXISTS (SELECT 1 FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id WHERE r.source_trial_id=p.trial_id AND s.short_name=p.source)"),
        ("error", "DUPLICATE_EVENT_HASH", "SELECT COUNT(*) FROM (SELECT event_hash FROM trial_events GROUP BY event_hash HAVING COUNT(*)>1)"),
        ("error", "DUPLICATE_LOGICAL_EVENT", "SELECT COUNT(*) FROM (SELECT record_id,field_name,COALESCE(old_value,''),COALESCE(new_value,''),detected_at FROM trial_events GROUP BY 1,2,3,4,5 HAVING COUNT(*)>1)"),
        ("error", "DUPLICATE_REGISTRY_VERSION", "SELECT COUNT(*) FROM (SELECT source_id,source_trial_id,version_number FROM registry_records GROUP BY 1,2,3 HAVING COUNT(*)>1)"),
        ("error", "INVALID_LATEST_VERSION", "SELECT COUNT(*) FROM registry_records r WHERE r.is_latest=1 AND r.version_number<>(SELECT MAX(v.version_number) FROM registry_records v WHERE v.source_id=r.source_id AND v.source_trial_id=r.source_trial_id)"),
        ("error", "DUPLICATE_MONITOR_OCCURRENCE", "SELECT COUNT(*) FROM (SELECT monitor_id,scheduled_for FROM monitor_runs WHERE scheduled_for IS NOT NULL GROUP BY 1,2 HAVING COUNT(*)>1)"),
        ("error", "DUPLICATE_INGESTION_OCCURRENCE", "SELECT COUNT(*) FROM (SELECT source_id,scheduled_for FROM registry_ingestion_runs WHERE scheduled_for IS NOT NULL GROUP BY 1,2 HAVING COUNT(*)>1)"),
        ("error", "DUPLICATE_NOTIFICATION", "SELECT COUNT(*) FROM (SELECT monitor_event_id,channel FROM notifications GROUP BY 1,2 HAVING COUNT(*)>1)"),
        ("error", "MULTIPLE_RUNNING_INGESTIONS", "SELECT COUNT(*) FROM (SELECT source_id FROM registry_ingestion_runs WHERE status='running' GROUP BY source_id HAVING COUNT(*)>1)"),
        ("error", "IMPOSSIBLE_INGESTION_STATE", "SELECT COUNT(*) FROM registry_ingestion_runs WHERE (status='running' AND finished_at IS NOT NULL) OR (status IN ('succeeded','failed','interrupted','partially_succeeded') AND finished_at IS NULL) OR (status='failed' AND cursor_after IS NOT NULL)"),
        ("error", "INVALID_NOTIFICATION_STATE", "SELECT COUNT(*) FROM notifications WHERE channel NOT IN ('in_app','email') OR status NOT IN ('pending','sending','delivered','failed')"),
        ("error", "INVALID_NOTIFICATION_READ_STATE", "SELECT COUNT(*) FROM notifications WHERE channel='email' AND read_at IS NOT NULL"),
    )
    for severity, code, sql in checks:
        count = conn.execute(sql).fetchone()[0]
        if count:
            add(severity, code, f"{count} affected row(s)", count)

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=stale_minutes)
    stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    for table, code, column in (
        ("registry_ingestion_runs", "STALE_INGESTION", "started_at"),
        ("monitor_runs", "STALE_MONITOR_RUN", "started_at"),
        ("notifications", "STALE_EMAIL_CLAIM", "claimed_at"),
    ):
        filter_sql = "status='sending' AND channel='email'" if table == "notifications" else "status='running'"
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {filter_sql} AND {column}<?", (stamp,)).fetchone()[0]
        if count:
            add("warning", code, f"{count} stale row(s)", count)

    from core.saved_searches import validate_state
    for row in conn.execute("SELECT id,state_json,search_schema_version FROM saved_searches ORDER BY id"):
        try:
            validate_state(json.loads(row["state_json"]), row["search_schema_version"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            add("error", "INVALID_SAVED_SEARCH", f"saved search {row['id']}: {exc}")
    return _result(issues)


def _result(issues: list[dict]) -> dict:
    summary = {key: sum(issue["count"] for issue in issues if issue["severity"] == key)
               for key in ("critical", "error", "warning", "info")}
    return {"status": "error" if summary["critical"] or summary["error"] else
            "warning" if summary["warning"] else "ok", "issues": issues, "summary": summary}
