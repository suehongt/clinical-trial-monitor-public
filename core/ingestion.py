"""Durable, canonical registry-ingestion orchestration (Phase 3F).

Collectors retain registry-specific fetch/parse/normalise logic.  This module
is the only place that turns a collector invocation into an operational run:
manual, retry and scheduled callers all use the same claim → run → finalize
path.  In particular, it never advances the successful watermark on failure.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Callable, Dict, Optional

from config import CONFIG
from core.redaction import redact
from db.connection import get_connection


# This is the operational source of truth: adapter identity, enabled state
# remains in registry_sources/config, while cadence is deliberately kept here
# with the orchestration policy that consumes it.
REGISTRY_CONFIG = {
    "clinicaltrials_gov": {"sync_every_hours": 24},
    "chictr": {"sync_every_hours": 24},
    "chinadrugtrials": {"sync_every_hours": 24},
    "who_ictrp": {"sync_every_hours": 24},
    "ctis": {"sync_every_hours": 24},
    "isrctn": {"sync_every_hours": 24},
    "euctr": {"sync_every_hours": None, "scheduled": False},
}
REGISTRY_KEYS = tuple(REGISTRY_CONFIG)

# Freshness windows scale with the source cadence plus this skew margin,
# so a daily source is fresh for cadence+6h (30h) — the previous hardcoded
# behaviour — while a slower cadence gets a proportionally wider window.
_FRESH_SKEW_HOURS = 6


def collector_for(registry: str):
    if registry == "clinicaltrials_gov":
        from collectors.clinicaltrials import ClinicalTrialsGovCollector
        return ClinicalTrialsGovCollector()
    if registry == "chictr":
        from collectors.chictr import ChiCTRCollector
        return ChiCTRCollector()
    if registry == "chinadrugtrials":
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        return ChinaDrugTrialsCollector()
    if registry == "who_ictrp":
        from collectors.who_ictrp import WHOICTRPCollector
        return WHOICTRPCollector()
    if registry == "ctis":
        from collectors.ctis import CTISCollector
        return CTISCollector()
    if registry == "isrctn":
        from collectors.isrctn import ISRCTNCollector
        return ISRCTNCollector()
    if registry == "euctr":
        from collectors.euctr import EUCTRCollector
        return EUCTRCollector()
    raise ValueError(f"unknown registry: {registry}")


def _utc(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def reconcile_interrupted_runs(now: Optional[datetime] = None, *, older_than_minutes: int = 120) -> int:
    """Make crashes observable instead of leaving permanent running rows."""
    cutoff = _utc((now or datetime.now(timezone.utc)) - timedelta(minutes=older_than_minutes))
    conn = get_connection()
    cur = conn.execute("""UPDATE registry_ingestion_runs
        SET status='interrupted', finished_at=?, failure_stage='interrupted',
            error_type='InterruptedRun', error_message='process did not finalize this run'
        WHERE status='running' AND started_at < ?""", (_utc(now), cutoff))
    conn.commit()
    return cur.rowcount


def run_registry_ingestion(
    registry: str,
    *,
    trigger: str = "manual",
    since: Optional[str] = None,
    force_full: bool = False,
    is_bootstrap: bool = False,
    retry_of_run_id: Optional[int] = None,
    scheduled_for: Optional[str] = None,
    collector: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Claim, execute and durably finalize one registry ingestion.

    ``collector`` is injectable for deterministic offline tests.  Its
    ``run(since=..., is_bootstrap=...)`` contract is the existing collector
    contract, so this wrapper does not duplicate fetch/normalise/versioning.
    """
    if registry not in REGISTRY_KEYS:
        raise ValueError(f"unknown registry: {registry}")
    if trigger not in {"manual", "scheduled", "retry"}:
        raise ValueError(f"invalid ingestion trigger: {trigger}")
    if trigger == "scheduled" and REGISTRY_CONFIG[registry].get("scheduled") is False:
        raise ValueError(f"registry {registry} is manual-only")
    if trigger != "scheduled" and scheduled_for is not None:
        raise ValueError("scheduled_for is only valid for scheduled ingestion")
    collector = collector or collector_for(registry)
    conn = get_connection()
    source_id = collector.source_id
    conn.execute("INSERT OR IGNORE INTO sync_status (source_id) VALUES (?)", (source_id,))
    if scheduled_for is not None:
        existing = conn.execute("SELECT run_id,status FROM registry_ingestion_runs WHERE source_id=? AND scheduled_for=?",
                                (source_id, scheduled_for)).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], "status": "already_claimed"}
    running = conn.execute("SELECT run_id FROM registry_ingestion_runs WHERE source_id=? AND status='running'", (source_id,)).fetchone()
    if running is not None:
        raise RuntimeError(f"registry {registry} already has running ingestion {running['run_id']}")
    state = conn.execute("SELECT last_successful_sync FROM sync_status WHERE source_id=?", (source_id,)).fetchone()
    cursor_before = state["last_successful_sync"] if state else None
    preexisting_records = conn.execute("SELECT COUNT(*) FROM registry_records WHERE source_id=? AND is_latest=1", (source_id,)).fetchone()[0]
    # Explicit full scans must not inherit the last successful cursor.
    effective_since = None if force_full else (since if since is not None else cursor_before)
    try:
        cur = conn.execute("""INSERT INTO registry_ingestion_runs
            (source_id, trigger, status, started_at, cursor_before, retry_of_run_id, scheduled_for)
            VALUES (?, ?, 'running', ?, ?, ?, ?)""",
            (source_id, trigger, _utc(now), cursor_before, retry_of_run_id, scheduled_for))
    except sqlite3.IntegrityError:
        # A second worker has claimed this exact scheduled occurrence.  The
        # existing durable run is authoritative; do not fetch twice.
        existing = conn.execute("SELECT run_id,status FROM registry_ingestion_runs WHERE source_id=? AND scheduled_for=?",
                                (source_id, scheduled_for)).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], "status": "already_claimed"}
        raise
    run_id = cur.lastrowid
    conn.execute("UPDATE sync_status SET last_attempted_sync=?, last_run_id=?, updated_at=? WHERE source_id=?",
                 (_utc(now), run_id, _utc(now), source_id))
    conn.commit()

    try:
        summary = collector.run(since=effective_since, is_bootstrap=is_bootstrap)
        status = str(summary.get("status", "succeeded"))
        if status == "failed":
            raise RuntimeError(str(summary.get("error_message") or "collector fetch failed"))
        # A non-empty local corpus plus an unexplained empty response is a
        # transport/parse failure, never evidence that a registry was wiped.
        # No absence-based deletion occurs in this project, and this turns the
        # otherwise ambiguous poll into an explicit failed operational run.
        if preexisting_records and summary.get("found", 0) == 0 and not getattr(collector, "allow_empty_response", False):
            raise RuntimeError("unexpected empty source response")
        # The BaseCollector has already committed versions/events before it
        # reports success.  Only now is it safe to commit the run cursor.
        represented = conn.execute("SELECT MAX(last_updated_at_source) FROM registry_records WHERE source_id=? AND is_latest=1", (source_id,)).fetchone()[0]
        cursor_after = conn.execute("SELECT last_successful_sync FROM sync_status WHERE source_id=?", (source_id,)).fetchone()[0] or _utc(now)
        final_status = "partially_succeeded" if status == "partially_succeeded" else "succeeded"
        conn.execute("""UPDATE registry_ingestion_runs SET finished_at=?, status=?, cursor_after=?,
            records_fetched=?, records_new=?, records_updated=?, records_unchanged=?, records_failed=?,
            trial_events_created=? WHERE run_id=?""",
            (_utc(now), final_status, cursor_after, summary.get("found"), summary.get("new"),
             summary.get("updated"), summary.get("skipped"), summary.get("failed", 0),
             summary.get("changes"), run_id))
        next_sync = None
        if trigger == "scheduled":
            next_sync = _utc((now or datetime.now(timezone.utc)) + timedelta(hours=REGISTRY_CONFIG[registry]["sync_every_hours"]))
        conn.execute("""UPDATE sync_status SET last_successful_sync=?, source_current_through=?,
            consecutive_failures=0, last_run_id=?, next_sync_at=COALESCE(?, next_sync_at), updated_at=? WHERE source_id=?""",
            (cursor_after, represented, run_id, next_sync, _utc(now), source_id))
        conn.commit()
        return {"run_id": run_id, "status": final_status, **summary}
    except Exception as exc:
        # Never write cursor_after or last_successful_sync here.  The prior
        # successful watermark remains retry's starting point.
        conn.execute("""UPDATE registry_ingestion_runs SET finished_at=?, status='failed',
            failure_stage='fetch_or_process', error_type=?, error_message=? WHERE run_id=?""",
            (_utc(now), type(exc).__name__, redact(exc)[:1000], run_id))
        conn.execute("""UPDATE sync_status SET last_successful_sync=?,
            consecutive_failures=consecutive_failures+1, last_run_id=?, updated_at=? WHERE source_id=?""",
                     (cursor_before, run_id, _utc(now), source_id))
        conn.commit()
        return {"run_id": run_id, "status": "failed", "error_type": type(exc).__name__, "error_message": redact(exc)}


def run_due_registry_ingestions(now: Optional[datetime] = None, *, runner: Callable[..., Dict[str, Any]] = run_registry_ingestion) -> list[Dict[str, Any]]:
    """Run each due registry occurrence once, independently of monitors.

    A source with no operational schedule is initialized as due now.  Manual
    syncs intentionally do not alter ``next_sync_at``; only a successful
    scheduled occurrence advances it.  The unique scheduled occurrence key
    makes duplicate ticks/restarts harmless.
    """
    current = now or datetime.now(timezone.utc)
    occurrence = _utc(current)
    conn = get_connection()
    results: list[Dict[str, Any]] = []
    for registry in REGISTRY_KEYS:
        if REGISTRY_CONFIG[registry].get("scheduled") is False:
            continue
        cfg = CONFIG.sources[registry]
        if not cfg.enabled:
            continue
        row = conn.execute("""SELECT ss.next_sync_at FROM sync_status ss
            JOIN registry_sources s ON s.source_id=ss.source_id WHERE s.short_name=?""",
                           (cfg.short_name,)).fetchone()
        due_at = row["next_sync_at"] if row else None
        if due_at and due_at > occurrence:
            continue
        results.append(runner(registry, trigger="scheduled", now=current, scheduled_for=occurrence))
    return results


def _stamp_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored UTC stamp ("YYYY-MM-DD HH:MM:SS" or ISO) as aware UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def registry_health(now: Optional[datetime] = None) -> list[Dict[str, Any]]:
    """Cheap per-registry health view; no run-history scan in the UI.

    Freshness is a statement about the library's data, not about the sync
    schedule: the decisive stamp is the newest honest evidence that this
    library holds current source data — the adapter's successful-sync
    cursor where it is written (NCT), else the newest crawled record
    (ChiCTR/CTR/ICTRP discovery-enrich path touches ``last_crawled_at``
    without advancing the cursor).  A no-op enrich must never read as
    stale, and a schedule edit must never read as fresh — only the data
    stamp decides.  Thresholds scale with the configured cadence.
    """
    current = now or datetime.now(timezone.utc)
    conn = get_connection()
    rows = [dict(r) for r in conn.execute("""SELECT s.source_id, s.short_name, s.full_name, s.enabled,
        ss.last_attempted_sync, ss.last_successful_sync, ss.source_current_through,
        ss.consecutive_failures, ss.next_sync_at, ss.last_run_id, ir.status latest_run_status,
        ir.records_fetched, ir.records_new, ir.records_updated
        FROM registry_sources s LEFT JOIN sync_status ss ON ss.source_id=s.source_id
        LEFT JOIN registry_ingestion_runs ir ON ir.run_id=ss.last_run_id
        ORDER BY s.source_id""").fetchall()]
    crawled = {r["source_id"]: r["newest"] for r in conn.execute(
        """SELECT source_id, MAX(last_crawled_at) newest FROM registry_records
           WHERE is_latest=1 GROUP BY source_id""").fetchall()}
    for row in rows:
        registry = next((key for key, cfg in CONFIG.sources.items() if cfg.short_name == row["short_name"]), None)
        cadence = REGISTRY_CONFIG.get(registry, {}).get("sync_every_hours") or 24
        stamp_dt = max((d for d in (_stamp_dt(row["last_successful_sync"]),
                                    _stamp_dt(crawled.get(row["source_id"]))) if d), default=None)
        stamp_text = stamp_dt.strftime("%Y-%m-%d %H:%M:%S") if stamp_dt else None
        if not row["enabled"]:
            freshness, reason = "disabled", "SOURCE_DISABLED"
        elif stamp_dt is None:
            freshness, reason = "unknown", "NO_DATA_SYNCED"
        else:
            age = (current - stamp_dt).total_seconds() / 3600
            fresh_limit = cadence + _FRESH_SKEW_HOURS
            delayed_limit = cadence * 3
            freshness = "fresh" if age <= fresh_limit else "delayed" if age <= delayed_limit else "stale"
            if freshness == "fresh":
                reason = "OK"
            elif (row["consecutive_failures"] or 0) > 0:
                reason = "SYNC_FAILURES"
            else:
                reason = "NO_RECENT_DATA"
        row["registry"] = registry
        row["sync_every_hours"] = REGISTRY_CONFIG.get(registry, {}).get("sync_every_hours")
        row["data_as_of"] = stamp_text
        row["stale_reason"] = reason
        row["freshness"] = freshness
        row["enabled"] = bool(row["enabled"])
    return rows
