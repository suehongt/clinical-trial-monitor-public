#!/usr/bin/env python3
"""
Global Clinical Trial Continuous Monitoring System — Unified Entry Point.

Usage:
    # Initialise database (idempotent)
    python run_monitor.py init

    # Enable a data source (by config key)
    python run_monitor.py enable clinicaltrials_gov

    # Run one crawl cycle for all enabled sources
    python run_monitor.py crawl

    # Run one crawl cycle for a specific source
    python run_monitor.py crawl --source clinicaltrials_gov

    # Run incremental crawl (only records updated since last run)
    python run_monitor.py crawl --incremental

    # Entity resolution: link unlinked records to master trials
    python run_monitor.py resolve

    # Generate daily report
    python run_monitor.py report --type daily

    # Show unacknowledged changes
    python run_monitor.py changes [--source NCT] [--category status]

    # Show database stats
    python run_monitor.py stats
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import CONFIG, BACKUP_DIR, ensure_dirs
from db.connection import get_connection, close_connection
from db.schema import create_schema, get_table_info

logger = logging.getLogger("ct_monitor")


def batch_crawl_sources(enabled_keys: list) -> list:
    """Reduce DB-enabled source keys to the batch-crawl ("all") set.

    Manual-only registries (REGISTRY_CONFIG scheduled=False, i.e. EUCTR's
    bounded historical backfill) are excluded — the nightly pipeline would
    otherwise re-download the same head of the register forever.  They are
    still crawlable by naming them: `crawl --source euctr --bootstrap`.
    """
    from core.ingestion import REGISTRY_CONFIG
    return [k for k in enabled_keys
            if REGISTRY_CONFIG.get(k, {}).get("scheduled") is not False]


# ── Setup ────────────────────────────────────────────────────────────────

def _init_logging() -> None:
    from logging.handlers import RotatingFileHandler

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    file_handler = RotatingFileHandler(
        str(CONFIG.log.file),
        maxBytes=CONFIG.log.max_mb * 1024 * 1024,
        backupCount=CONFIG.log.backup_count,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=getattr(logging, CONFIG.log.level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            file_handler,
        ],
    )


# ── CLI commands ─────────────────────────────────────────────────────────

def cmd_init(args) -> None:
    """Create all tables and seed lookup data."""
    create_schema()
    tables = get_table_info()
    print("Database initialised.  Tables:")
    for t in tables:
        print(f"  {t['name']:35s}  {t['rows']} rows")


def cmd_version(args) -> None:
    from core.version import APP_VERSION, SCHEMA_VERSION, build_id
    print(f"Clinical Trial Monitor {APP_VERSION}\nSchema v{SCHEMA_VERSION}\nBuild {build_id()}")


def cmd_doctor(args) -> None:
    """Read-only deployment checks; missing requirements yield exit status 1."""
    from core.integrity import open_readonly, startup_check, audit
    from core.email_provider import validate_email_configuration
    checks = []
    def check(name, ok, detail=""):
        checks.append(ok)
        print(f"[{'OK' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")
    check("Python runtime", sys.version_info >= (3, 12), sys.version.split()[0] + " (requires 3.12+)")
    check("Database file", CONFIG.db.path.is_file(), str(CONFIG.db.path))
    if CONFIG.db.path.is_file():
        try:
            with closing(open_readonly(CONFIG.db.path)) as conn:
                state = startup_check(conn)
                result = audit(conn)
            check("Schema", True, f"v{state['schema_version']}")
            check("Integrity", result["status"] == "ok", str(result["summary"]))
        except Exception as exc:
            check("Database compatibility", False, str(exc))
    for label, path in (("Database directory", CONFIG.db.path.parent),
                        ("Backup directory", BACKUP_DIR), ("Log directory", CONFIG.log.file.parent)):
        check(label, path.is_dir() and os.access(path, os.W_OK), str(path))
    assets = Path(__file__).resolve().parent / "web" / "dist" / "index.html"
    check("Frontend assets", assets.is_file(), str(assets))
    try:
        validate_email_configuration()
        check("SMTP configuration", True)
    except Exception as exc:
        check("SMTP configuration", False, str(exc))
    if not all(checks):
        raise SystemExit(1)


def cmd_status(args) -> None:
    """Safe local snapshot of runtime data; no network polling or secrets."""
    import json
    from core.version import APP_VERSION, build_id
    from core.integrity import open_readonly, startup_check
    with closing(open_readonly(CONFIG.db.path)) as conn:
        state = startup_check(conn)
        pending = conn.execute("SELECT COUNT(*) FROM notifications WHERE channel='email' AND status='pending'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM notifications WHERE channel='email' AND status='failed'").fetchone()[0]
        sources = [dict(r) for r in conn.execute("""SELECT s.short_name,ss.last_successful_sync
            FROM registry_sources s LEFT JOIN sync_status ss ON ss.source_id=s.source_id ORDER BY s.short_name""")]
    print(json.dumps({"app_version": APP_VERSION, "build_id": build_id(),
                      "schema_version": state["schema_version"],
                      "database": str(CONFIG.db.path), "ready": True,
                      "scheduler": "external CLI", "email_pending": pending,
                      "email_failed": failed, "sources": sources}, ensure_ascii=False, indent=2))


def cmd_serve(args) -> None:
    """Prepare a single-node database, then serve API and built frontend."""
    from core.version import APP_VERSION, SCHEMA_VERSION
    from db.backup import backup_database
    if sys.version_info < (3, 12):
        raise RuntimeError("production serve requires Python 3.12 or newer")
    mode = os.environ.get("CT_MODE", "production").lower()
    if mode not in {"development", "production", "test"}:
        raise RuntimeError("CT_MODE must be development, production or test")
    host = os.environ.get("CT_HOST", "127.0.0.1")
    if mode == "production" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("production mode must bind localhost")
    port = int(os.environ.get("CT_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise RuntimeError("CT_PORT must be between 1 and 65535")
    if not (Path(__file__).resolve().parent / "web/dist/index.html").is_file():
        raise RuntimeError("production frontend assets missing; build web/dist before serving")
    if mode == "production":
        from core.email_provider import validate_email_configuration
        validate_email_configuration()
    path = CONFIG.db.path
    if mode == "production" and not os.environ.get("CT_DATA_DIR") and not os.environ.get("CT_DB_PATH"):
        raise RuntimeError("production startup needs CT_DATA_DIR or CT_DB_PATH; choose a persistent writable data location")
    prior_version = 0
    had_tables = False
    if path.is_file():
        with closing(sqlite3.connect(str(path))) as probe:
            had_tables = probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is not None
            if probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
                row = probe.execute("SELECT MAX(version) FROM schema_version").fetchone()
                prior_version = int(row[0] or 0)
    if prior_version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{prior_version} is newer than supported v{SCHEMA_VERSION}")
    if had_tables and prior_version < SCHEMA_VERSION:
        label = f"pre-upgrade-v{prior_version}-{APP_VERSION}"
        backup = backup_database(path, BACKUP_DIR, keep=1000000, label=label)
        print(f"Verified pre-migration backup: {backup}")
    create_schema()
    from core.integrity import open_readonly, startup_check, audit
    with closing(open_readonly(path)) as conn:
        startup_check(conn)
        result = audit(conn)
    if result["status"] != "ok":
        raise RuntimeError(f"database integrity audit failed after startup migration: {result['summary']}")
    os.environ.setdefault("CT_MODE", "production")
    os.environ["CT_DB_PATH"] = str(path)
    print(f"Clinical Trial Monitor {APP_VERSION}; schema v{SCHEMA_VERSION}; mode={os.environ['CT_MODE']}; host={host}; port={port}; database={path}; scheduler=external CLI; smtp={'enabled' if os.environ.get('CT_EMAIL_SMTP_HOST') else 'disabled'}")
    from server.__main__ import main as serve_main
    serve_main()


def cmd_enable(args) -> None:
    """Enable a data source in the database."""
    key = args.source
    if key not in CONFIG.sources:
        print(f"Unknown source: {key}.  Available: {', '.join(CONFIG.sources.keys())}")
        sys.exit(1)
    cfg = CONFIG.sources[key]
    cfg.enabled = True
    conn = get_connection()
    conn.execute(
        "UPDATE registry_sources SET enabled = 1 WHERE short_name = ?",
        (cfg.short_name,),
    )
    conn.commit()
    print(f"Enabled source: {cfg.name} ({cfg.short_name})")


def cmd_crawl(args) -> None:
    """Run collectors for enabled sources."""
    from collectors.clinicaltrials import ClinicalTrialsGovCollector
    from collectors.chictr import ChiCTRCollector
    from collectors.chinadrugtrials import ChinaDrugTrialsCollector
    from collectors.who_ictrp import WHOICTRPCollector
    from collectors.ctis import CTISCollector
    from collectors.isrctn import ISRCTNCollector
    from collectors.euctr import EUCTRCollector

    COLLECTOR_MAP = {
        "clinicaltrials_gov": ClinicalTrialsGovCollector,
        "chictr": ChiCTRCollector,
        "chinadrugtrials": ChinaDrugTrialsCollector,
        "who_ictrp": WHOICTRPCollector,
        "ctis": CTISCollector,
        "isrctn": ISRCTNCollector,
        "euctr": EUCTRCollector,
    }

    sources_to_run: list[str] = []
    if args.source and args.source != "all":
        if args.source not in COLLECTOR_MAP:
            print(f"Unknown source: {args.source}")
            sys.exit(1)
        sources_to_run = [args.source]
    else:
        # Read enabled sources from DB (persisted across invocations)
        conn = get_connection()
        short_to_key = {v.short_name: k for k, v in CONFIG.sources.items()}
        cur = conn.execute("SELECT short_name FROM registry_sources WHERE enabled = 1")
        for row in cur:
            key = short_to_key.get(row["short_name"])
            if key:
                sources_to_run.append(key)
        sources_to_run = batch_crawl_sources(sources_to_run)

    if not sources_to_run:
        print("No sources enabled.  Use `python run_monitor.py enable <source>` first.")
        print(f"Available: {', '.join(COLLECTOR_MAP.keys())}")
        sys.exit(1)

    for key in sources_to_run:
        cls = COLLECTOR_MAP[key]
        collector = cls()
        if getattr(args, "query_cond", None):
            # explicit override (multi-project pipeline crawls)
            collector.cfg.extra["query_cond"] = args.query_cond

        # Determine sync mode per source
        since: Optional[str] = None
        is_bootstrap = args.bootstrap

        if args.incremental and not is_bootstrap:
            conn = get_connection()
            cur = conn.execute(
                "SELECT last_successful_sync FROM sync_status WHERE source_id = ?",
                (collector.source_id,),
            )
            row = cur.fetchone()
            if row and row["last_successful_sync"]:
                since = row["last_successful_sync"]
                print(f"Incremental mode: fetching records updated since {since}")
                # Warn when the baseline is older than the staleness horizon —
                # an incremental diff on a very old baseline can miss records.
                try:
                    last_dt = datetime.fromisoformat(row["last_successful_sync"])
                    age_days = (datetime.utcnow() - last_dt).total_seconds() / 86400
                except ValueError:
                    age_days = None
                if age_days is not None and age_days > CONFIG.sync.max_staleness_days:
                    print(
                        f"  ⚠  last successful sync is {age_days:.0f} days old "
                        f"(max_staleness_days={CONFIG.sync.max_staleness_days}). "
                        "Consider `crawl --bootstrap` for a full refresh."
                    )
            else:
                print("No prior successful sync found; running full fetch.")
        elif is_bootstrap:
            print("Bootstrap mode: performing initial backfill (records will be marked is_bootstrap=1)")

        print(f"\n{'='*60}")
        print(f"Running collector: {collector.cfg.name}")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"Started at: {now}")
        print(f"{'='*60}")

        try:
            # 中文源发现层的三段式分步开关（design
            # chinese-source-crawl-upgrade-plan.md §2.4）；NCT 等无 discovery
            # 层的源自动回退常规 run()
            if getattr(args, "discover_only", False) and hasattr(collector, "discover_new"):
                stats = collector.discover_new(since=since)
                print(f"  Discovered: {stats['queued']} queued "
                      f"({stats['pages_walked']} pages, {stats['stopped_reason']})")
                continue
            if getattr(args, "enrich_only", False) and hasattr(collector, "enrich_pending"):
                kws = None
                if getattr(args, "enrich_keywords", None):
                    kws = [k.strip() for k in args.enrich_keywords.split(",")
                           if k.strip()]
                stats = collector.enrich_pending(keywords=kws)
                extra = (f"  keyword-matched: {stats['keyword_matched']}"
                         if kws and "keyword_matched" in stats else "")
                print(f"  Enriched: {stats['enriched']}  failed: {stats['failed']}"
                      f"  skipped: {stats['skipped']}{extra}")
                continue
            if getattr(args, "legacy_discovery", False) and hasattr(collector, "fetch_new_or_updated_legacy"):
                collector.use_legacy_discovery = True

            if getattr(args, "refresh_only", False):
                # 复查队列：先入队一批久未重访的记录，再按预算重访详情页。
                # 仅对提供 refresh_one 钩子的 WAF 源生效。
                if not hasattr(collector, "refresh_one"):
                    print("  Source has no refresh hook; skipping.")
                    continue
                from core.refresh import plan_refresh, refresh_pending
                queued = plan_refresh()
                stats = refresh_pending(collector, limit=args.refresh_limit)
                print(f"  Refresh:  planned {queued}  refreshed: {stats['refreshed']}"
                      f"  failed: {stats['failed']}  skipped: {stats['skipped']}"
                      f"  changes: {stats['changes']}")
                continue

            # Standard crawls go through the durable Phase 3F service.  The
            # specialised discovery/enrichment/refresh maintenance commands
            # above retain their dedicated queue semantics.
            from core.ingestion import run_registry_ingestion
            summary = run_registry_ingestion(
                key, trigger="manual", since=since,
                is_bootstrap=is_bootstrap, collector=collector,
                force_full=not args.incremental or is_bootstrap,
            )
            if summary.get("status") == "failed":
                print(f"  ✗  Failed: {summary.get('error_message', 'unknown error')}")
                continue
            print(f"  Found:   {summary['found']}")
            print(f"  New:     {summary['new']}")
            print(f"  Updated: {summary['updated']}")
            print(f"  Skipped: {summary['skipped']}")
            print(f"  Changes: {summary['changes']}")
        except NotImplementedError as e:
            print(f"  ⚠  {e}")
        except Exception as e:
            logger.exception("Collector %s failed", key)
            print(f"  ✗  Failed: {e}")


def cmd_resolve(args) -> None:
    """Auto-resolve unlinked records to master trials."""
    from core.entity_resolution import auto_resolve
    print("Running entity resolution …")
    summary = auto_resolve()
    print(f"  Total unlinked records:          {summary['total']}")
    print(f"  Linked to existing master:       {summary['linked']}")
    print(f"  Created new master trials:        {summary['created']}")
    created_agg = summary.get("created_via_aggregator", 0)
    if created_agg:
        print(f"  Created via AGGREGATOR:           {created_agg}")
    print(f"  Queued for review:                {summary['queued']}")
    print(f"  Skipped (errors):                 {summary['skipped']}")
    if summary['queued']:
        print(f"\n  ⚠  {summary['queued']} record(s) queued for manual review.")
        print("  Use `python run_monitor.py review_queue list` to review.")


def cmd_report(args) -> None:
    """Generate a report."""
    from core.reporting import generate_daily_report, generate_weekly_report
    rtype = args.type or "daily"
    print(f"Generating {rtype} report …")
    if rtype == "daily":
        result = generate_daily_report()
    elif rtype == "weekly":
        result = generate_weekly_report()
    else:
        print(f"Unknown report type: {rtype}.  Use 'daily' or 'weekly'.")
        sys.exit(1)

    if result.get("skipped"):
        print(f"  Report already exists (id={result['report_id']}).  Use --force to regenerate.")
    else:
        print(f"  New records:       {result['new_records']}")
        print(f"  Updated records:   {result['updated_records']}")
        print(f"  New master trials: {result['new_master_trials']}")
        print(f"  Change events:     {result['change_events']}")
        print(f"  Acknowledged:      {result['acknowledged']}")
        if result.get("new_aggregator_records"):
            print(f"  AGGREGATOR records: {result['new_aggregator_records']} (not counted as new discoveries)")


def cmd_review_queue(args) -> None:
    """Manage the resolution review queue."""
    from core.entity_resolution import (
        get_review_queue, approve_match, reject_match,
        queue_stats, get_queue_batch, batch_resolve, export_queue_csv,
    )

    if args.rq_action == "list":
        status = args.rq_status or "pending"
        items = get_review_queue(status=status)
        if not items:
            print(f"No {status} items in the resolution queue.")
            return
        print(f"Resolution Queue ({status} items: {len(items)}):")
        print(f"{'ID':>5}  {'Source':8}  {'Trial ID':20}  {'Method':30}  {'Conf':6}  {'Reasoning'}")
        print("-" * 110)
        for q in items:
            rid_short = (q["source_trial_id"][:18] + "..") if len(q["source_trial_id"] or "") > 20 else (q["source_trial_id"] or "")
            method_short = q["match_method"][:28] if q["match_method"] else ""
            reasoning_short = (q["reasoning"] or "")[:40]
            print(f"{q['queue_id']:>5}  {q['source_name']:8}  {rid_short:20}  "
                  f"{method_short:30}  {q['confidence']:.2f}  {reasoning_short}")

    elif args.rq_action == "approve":
        if not args.rq_id:
            print("Usage: review_queue approve --id <queue_id>")
            return
        success = approve_match(args.rq_id, reviewer=args.rq_reviewer)
        if success:
            print(f"Queue entry {args.rq_id} approved — record linked to master trial.")
        else:
            print(f"Failed to approve queue entry {args.rq_id}.")

    elif args.rq_action == "reject":
        if not args.rq_id:
            print("Usage: review_queue reject --id <queue_id>")
            return
        success = reject_match(args.rq_id, reviewer=args.rq_reviewer)
        if success:
            print(f"Queue entry {args.rq_id} rejected.")
            print("  The record remains unlinked. Run `resolve` again to create a new master trial.")
        else:
            print(f"Failed to reject queue entry {args.rq_id}.")

    elif args.rq_action == "stats":
        stats = queue_stats()
        print(f"Resolution Queue — pending: {stats['total']} item(s)")
        for label, count in stats["buckets"].items():
            print(f"  conf {label:>10}: {count}")
        print(f"  earliest created: {stats['earliest_created'] or '-'}")
        print(f"  latest created:   {stats['latest_created'] or '-'}")

    elif args.rq_action == "batch":
        items = get_queue_batch(
            min_confidence=args.rq_min_conf,
            max_confidence=args.rq_max_conf,
            limit=args.rq_limit,
        )
        if not items:
            print("No pending items match the given confidence range.")
            return
        confs = [q["confidence"] for q in items]
        action = args.rq_batch_action
        verb = "approve" if action == "approve" else "reject"
        print(f"Batch {verb}: {len(items)} pending item(s) selected, "
              f"confidence range {min(confs):.2f} - {max(confs):.2f}.")
        if action == "approve":
            print("  ⚠  Approving merges records across sources — mis-merges are hard to undo.")
        print(f"Sample (first {min(10, len(items))} of {len(items)}):")
        for q in items[:10]:
            rid_short = (q["source_trial_id"][:18] + "..") if len(q["source_trial_id"] or "") > 20 else (q["source_trial_id"] or "")
            reasoning_short = (q["reasoning"] or "")[:40]
            print(f"  {q['queue_id']:>5}  {q['source_name']:8}  {rid_short:20}  "
                  f"{q['confidence']:.2f}  {reasoning_short}")
        if not getattr(args, "rq_yes", False):
            print("DRY-RUN: nothing written to the database. Re-run with --yes to apply.")
            return
        succeeded, failures = batch_resolve(
            [q["queue_id"] for q in items], action, reviewer=args.rq_reviewer,
        )
        print(f"Batch {verb} done: {succeeded} succeeded, {len(failures)} failed.")
        for qid, err in failures[:10]:
            print(f"  ✗  queue {qid}: {err}")

    elif args.rq_action == "export":
        count = export_queue_csv(
            args.rq_output,
            min_confidence=args.rq_export_min_conf,
            status=args.rq_export_status,
        )
        print(f"Exported {count} queue item(s) to {args.rq_output} (utf-8-sig CSV).")


def cmd_changes(args) -> None:
    """Show unacknowledged changes (from trial_events table)."""
    from core.change_detection import get_unacknowledged_changes
    changes = get_unacknowledged_changes(
        source_short_name=args.source,
        category=args.category,
        limit=args.limit,
    )
    if not changes:
        print("No unacknowledged changes.")
        return
    print(f"Unacknowledged changes ({len(changes)}):")
    print(f"{'ID':>6}  {'Source':8}  {'Trial ID':20}  {'Field':25}  {'Category':12}  {'Detected'}")
    print("-" * 100)
    for c in changes:
        print(f"{c['event_id']:>6}  {c['source_name']:8}  {c['source_trial_id']:20}  "
              f"{c['field_name']:25}  {c['change_category']:12}  {c['detected_at']}")


def cmd_stats(args) -> None:
    """Show database statistics."""
    from db.schema import create_schema

    # Idempotent: stats on a fresh checkout should not print "-1 rows"
    create_schema()
    tables = get_table_info()
    conn = get_connection()

    print("Database Statistics")
    print("=" * 50)
    for t in tables:
        print(f"  {t['name']:35s}  {t['rows']:>8} rows")

    print()
    # Unlinked records
    cur = conn.execute(
        "SELECT count(*) FROM registry_records r WHERE r.is_latest = 1 "
        "AND r.record_id NOT IN (SELECT record_id FROM record_master_map)"
    )
    unlinked = cur.fetchone()[0]
    print(f"  Unlinked latest records: {unlinked}")

    # Unacknowledged trial_events
    cur = conn.execute("SELECT count(*) FROM trial_events WHERE acknowledged = 0")
    print(f"  Unacknowledged trial events: {cur.fetchone()[0]}")

    # Unacknowledged legacy change_events
    cur = conn.execute("SELECT count(*) FROM change_events WHERE acknowledged = 0")
    print(f"  Unacknowledged legacy changes: {cur.fetchone()[0]}")

    # Resolution queue
    cur = conn.execute("SELECT count(*) FROM resolution_queue WHERE status = 'pending'")
    print(f"  Pending resolution queue items: {cur.fetchone()[0]}")

    # Sync status
    print()
    cur = conn.execute("""
        SELECT s.short_name, ss.last_attempted_sync, ss.last_successful_sync,
               ss.bootstrap_completed, ss.last_record_count
        FROM sync_status ss
        JOIN registry_sources s ON s.source_id = ss.source_id
        ORDER BY s.short_name
    """)
    print("  Sync Status Per Source:")
    for row in cur:
        boot = "✓" if row["bootstrap_completed"] else " "
        print(f"    {row['short_name']:8}  "
              f"bootstrapped={boot}  "
              f"last_ok={row['last_successful_sync'] or 'never':20}  "
              f"records={row['last_record_count'] or 0}")

    # Last crawl per source
    print()
    cur = conn.execute("""
        SELECT s.short_name, cl.status, cl.started_at, cl.completed_at,
               cl.records_new, cl.records_updated
        FROM crawl_log cl
        JOIN registry_sources s ON s.source_id = cl.source_id
        WHERE cl.log_id IN (SELECT MAX(log_id) FROM crawl_log GROUP BY source_id)
        ORDER BY cl.started_at DESC
    """)
    print("  Last Crawl Per Source:")
    for row in cur:
        print(f"    {row['short_name']:8}  {row['status']:10}  "
              f"new={row['records_new']}  updated={row['records_updated']}  "
              f"at={row['completed_at'] or row['started_at']}")


# ── Main ─────────────────────────────────────────────────────────────────

def cmd_digest(args) -> None:
    """Push a daily digest (new trials + field changes) to notify channels."""
    from core.digest import run_digest

    result = run_digest(
        dry_run=getattr(args, "dry_run", False),
        english=getattr(args, "english", False),
        limit=getattr(args, "limit", 500),
    )
    print(f"[digest] status: {result['status']}  "
          f"new: {result['new']}  changed: {result['changed']}")
    if result.get("results"):
        print(f"[digest] channels: {result['results']}")
    if result["text"]:
        print("\n" + result["text"])
        print()


def cmd_backup(args) -> None:
    """Back up the SQLite database (online backup API, WAL-safe)."""
    import config
    from db.backup import backup_database, list_backups
    from db.recovery import verify_database

    out_dir = str(Path(args.out or config.BACKUP_DIR).expanduser())
    path = backup_database(CONFIG.db.path, out_dir,
                           keep=args.keep, label=args.label)
    verify_database(path)
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"Backup written: {path} ({size_mb:.1f} MB)")
    kept = list_backups(out_dir, label=args.label)
    print(f"Retention: keeping {len(kept)} backup(s) (max {args.keep})")


def cmd_integrity_check(args) -> None:
    import json
    from core.integrity import audit, open_readonly
    with closing(open_readonly(args.db or CONFIG.db.path)) as conn:
        result = audit(conn)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Integrity: {result['status']} {result['summary']}")
        for issue in result["issues"]:
            print(f"  {issue['severity'].upper()} {issue['code']}: {issue['detail']}")
    if result["summary"]["critical"] or result["summary"]["error"]:
        raise SystemExit(1)


def cmd_restore(args) -> None:
    from db.recovery import restore_database
    restored = restore_database(args.input, args.db or CONFIG.db.path, offline=args.offline)
    print(f"Restored and verified: {restored}")


def cmd_diagnostics(args) -> None:
    import json
    from core.integrity import open_readonly, startup_check
    path = Path(args.db or CONFIG.db.path)
    with closing(open_readonly(path)) as conn:
        state = startup_check(conn)
        tables = ("registry_records", "trial_events", "research_projects", "monitors",
                  "notifications", "registry_ingestion_runs")
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in tables}
    print(json.dumps({"schema_version": state["schema_version"],
                      "database_bytes": path.stat().st_size, "counts": counts}, indent=2))


def cmd_recover_stale(args) -> None:
    import json
    from core.recovery import reconcile_stale
    result = reconcile_stale(get_connection(), older_than_minutes=args.minutes)
    print(json.dumps(result))


def cmd_tick_monitors(args) -> None:
    """Run every topic monitor whose scheduled occurrence is due.

    Designed to be invoked on a short interval (launchd StartInterval);
    per-run failures are recorded in monitor_runs, not raised, so the tick
    itself only fails when the database is unreachable.
    """
    import json
    from core.monitor_scheduler import run_due_monitors
    result = run_due_monitors(get_connection(), now=datetime.now(timezone.utc))
    print(json.dumps(result))


def _report_step_flags(english: bool, chinese: bool) -> list[str]:
    """CLI flags for the pipeline report step (pure helper, no side effects).

    Default-to-English rule: with neither flag given the report step runs
    in English — Chinese reports need the LLM translation pass, which is
    expensive (DEEPSEEK_API_KEY) or degraded (dictionary fallback), so it
    must not be the silent default of a daily automation.  Explicit flags
    win over the default: ``--english`` always yields English, and when
    both flags are passed the conflict also resolves to English (with
    cmd_pipeline printing a warning about it).
    """
    if chinese and not english:
        return []
    return ["--english"]


def _notify_pipeline_results(results: list, level: str = "error") -> None:
    """Push a pipeline step summary to every configured notify channel.

    Best-effort by design: a notification failure must never turn a pipeline
    run into a different exit code or mask the real failed step, so the whole
    flow is guarded.  With no channels configured (CT_NOTIFY_* env vars) this
    prints a hint and moves on.
    """
    try:
        from core.notify import (configured_channels, format_pipeline_message,
                                 send_notification)
        if configured_channels():
            title, text = format_pipeline_message(results)
            outcomes = send_notification(title, text, level=level)
            sent = [c for c, st in outcomes.items() if st == "ok"]
            failed = [c for c, st in outcomes.items() if st == "failed"]
            msg = f"[pipeline] alert ({level}) sent via: {', '.join(sent) or 'none'}"
            if failed:
                msg += f" (delivery failed: {', '.join(failed)})"
            print(msg)
        else:
            print("[pipeline] no notify channels configured (CT_NOTIFY_* env "
                  "vars) — skipping alert")
    except Exception as e:
        print(f"[pipeline] notification skipped ({e})")


def _watch_keywords() -> list:
    """User-registered watch keywords (schema v9 watch_keywords table).

    Best-effort: a pre-v9 database or an unreachable DB simply yields an
    empty list — the pipeline must not break because subscriptions are
    unavailable.  Read-only access.
    """
    try:
        from db.connection import get_connection
        return [r["keyword"] for r in get_connection().execute(
            "SELECT keyword FROM watch_keywords ORDER BY watch_id").fetchall()]
    except sqlite3.OperationalError:
        return []


def cmd_pipeline(args) -> None:
    """One-command daily flow: crawl -> resolve -> quality -> export -> report -> backup.

    Each step runs in a subprocess with CT_DISEASE_PROFILE set, so the
    profile switch (and its imports) is isolated per step.
    """
    import subprocess
    import time
    import config

    profiles = ([k.strip() for k in args.profiles.split(",")]
                if getattr(args, "profiles", None) else [args.profile])
    if profiles == ["all"]:
        profiles = list(config.DISEASE_PROFILES)
    env = dict(os.environ, CT_DISEASE_PROFILE=profiles[0])
    py = sys.executable
    step_results: list[dict] = []

    def step(label, cmd, step_env=None) -> bool:
        print(f"\n{'=' * 60}\n[pipeline] {label}\n{'=' * 60}")
        started = time.time()
        r = subprocess.run(cmd, env=step_env or env)
        dt = time.time() - started
        ok = r.returncode == 0
        print(f"[pipeline] {label}: {'ok' if ok else 'FAILED'} ({dt:.0f}s)")
        step_results.append({"step": label, "ok": ok, "seconds": round(dt, 1)})
        return ok

    print(f"Pipeline started: profiles={','.join(profiles)}")
    ok = True

    if not args.skip_crawl:
        cmd = [py, "run_monitor.py", "crawl", "--incremental",
               "--source", args.source]
        watch_kws = _watch_keywords()
        if len(profiles) > 1 or watch_kws:
            # single crawl with the OR-union of every profile's query —
            # populates the DB for all profiles in one pass (per-source
            # incremental sync state stays consistent).  This branch also
            # covers a single profile when watches exist: subscriptions must
            # not silently disappear from the default pipeline invocation.
            # User watch keywords (schema v9) are OR-joined too, so subscribed
            # terms are crawled alongside the built-in profiles (NCT public
            # API only; WAF sources keep their discovery queues).
            query = config.combined_query(profiles)
            if watch_kws:
                query = query + " OR " + " OR ".join(watch_kws)
            cmd += ["--query-cond", query]
        ok = step("Crawl (incremental, combined query)", cmd) and ok

    ok = step("Entity resolution", [py, "run_monitor.py", "resolve"]) and ok

    if not getattr(args, "skip_quality", False):
        # R4 quality gate (false/missed-merge candidates etc.) right after
        # resolve validates the freshly-linked state before outputs are
        # generated.  Findings are informational — only a crash fails the step.
        ok = step("Quality checks (R4)", [py, "scripts/quality_report.py"]) and ok

    if len(profiles) > 1:
        ok = step("Export JSON (web viewer, all profiles)",
                  [py, "scripts/export_report_json.py", "--all"]) and ok
    else:
        ok = step("Export JSON (web viewer)",
                  [py, "scripts/export_report_json.py", "--profile", profiles[0]]) and ok

    chinese = getattr(args, "chinese", False)
    if args.english and chinese:
        print("[pipeline] WARNING: both --english and --chinese given; "
              "--english wins for the report step (conflict).")
    elif not args.english and not chinese:
        print("[pipeline] report step: defaulting to English reports "
              "(Chinese needs the LLM translation pass; pass --chinese to force)")

    for prof in profiles:
        report_cmd = [py, "scripts/trial_report.py"]
        report_cmd.extend(_report_step_flags(args.english, chinese))
        if args.full:
            report_cmd.append("--force")
        ok = step(f"Generate report ({prof})",
                  report_cmd, step_env=dict(env, CT_DISEASE_PROFILE=prof)) and ok

    if not getattr(args, "skip_digest", False):
        # 每日推送：报告生成后、备份前执行。digest 文案为内置中文
        # （FIELD_LABELS 双语原生格式化，无需报告步的 LLM 翻译管线）；
        # 未配置任何 CT_NOTIFY_* 渠道时内部 no-op 成功返回。
        ok = step("Notify digest", [py, "run_monitor.py", "digest"]) and ok

    if not getattr(args, "skip_backup", False):
        # after every DB writer (crawl/resolve/report checkpoint) — the
        # online backup API captures a consistent committed state
        ok = step("Database backup",
                  [py, "run_monitor.py", "backup",
                   "--out", str(config.BACKUP_DIR)]) and ok

    print(f"\nPipeline {'completed' if ok else 'finished with ERRORS'} "
          f"(profiles={','.join(profiles)})")

    # Alerts: failure always notifies (if channels are configured); the
    # success brief is opt-in so a healthy daily run stays quiet.
    notify_success = getattr(args, "notify_success", False)
    if not ok:
        _notify_pipeline_results(step_results, level="error")
    elif notify_success:
        _notify_pipeline_results(step_results, level="info")

    if not ok:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Global Clinical Trial Continuous Monitoring System",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="Print application and supported schema versions")
    p_version.set_defaults(func=cmd_version)
    p_serve = sub.add_parser("serve", help="Back up an older DB, migrate safely, audit, then serve the production API and frontend")
    p_serve.set_defaults(func=cmd_serve)
    p_doctor = sub.add_parser("doctor", help="Check deployment prerequisites without changing database data")
    p_doctor.set_defaults(func=cmd_doctor)
    p_status = sub.add_parser("status", help="Show local version, schema, source and delivery state without secrets")
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser("init", help="Create DB schema and seed lookup data")
    p_init.set_defaults(func=cmd_init)

    p_enable = sub.add_parser("enable", help="Enable a data source")
    p_enable.add_argument("source", choices=list(CONFIG.sources.keys()))
    p_enable.set_defaults(func=cmd_enable)

    p_crawl = sub.add_parser("crawl", help="Run collectors")
    p_crawl.add_argument("--source", default="all",
                         help="Source key (default: all enabled sources)")
    p_crawl.add_argument("--incremental", action="store_true",
                         help="Only fetch records updated since last successful sync")
    p_crawl.add_argument("--bootstrap", action="store_true",
                         help="Initial backfill — marks records as bootstrap (excluded from report counts)")
    p_crawl.add_argument("--query-cond", default=None, dest="query_cond",
                         help="Override the profile's condition query (e.g. the combined "
                              "query from config.combined_query for multi-project crawls)")
    p_crawl.add_argument("--discover-only", action="store_true",
                         help="ChiCTR/CTR only: run the discovery walk (list pages + "
                              "watermark) and queue new registrations, without fetching "
                              "detail pages")
    p_crawl.add_argument("--enrich-only", action="store_true",
                         help="ChiCTR/CTR only: consume the discovery queue (fetch detail "
                              "pages within the enrich_batch_size budget) without discovery")
    p_crawl.add_argument("--enrich-keywords", dest="enrich_keywords", default=None,
                         help="With --enrich-only: comma-separated keywords — pending items "
                              "whose captured list-page title matches are enriched first "
                              "(budget backfilled with non-matching items)")
    p_crawl.add_argument("--legacy-discovery", action="store_true",
                         help="ChiCTR/CTR only: fall back to the legacy keyword-search "
                              "discovery path (fallback switch for the new watermark walk)")
    p_crawl.add_argument("--refresh-only", dest="refresh_only", action="store_true",
                         help="ChiCTR/CTR only: re-visit detail pages of already-enriched "
                              "records via the refresh queue (plan_refresh + consume, "
                              "enrich_batch_size budget) so field updates get detected")
    p_crawl.add_argument("--refresh-limit", dest="refresh_limit", type=int, default=None,
                         help="With --refresh-only: max detail pages this round "
                              "(default: enrich_batch_size)")
    p_crawl.set_defaults(func=cmd_crawl)

    p_resolve = sub.add_parser("resolve", help="Auto-link records to master trials")
    p_resolve.set_defaults(func=cmd_resolve)

    p_report = sub.add_parser("report", help="Generate report")
    p_report.add_argument("--type", choices=["daily", "weekly"], default="daily")
    p_report.set_defaults(func=cmd_report)

    p_changes = sub.add_parser("changes", help="Show unacknowledged changes")
    p_changes.add_argument("--source", default=None, help="Filter by source short name")
    p_changes.add_argument("--category", default=None, help="Filter by change category")
    p_changes.add_argument("--limit", type=int, default=50, help="Max rows")
    p_changes.set_defaults(func=cmd_changes)

    p_digest = sub.add_parser(
        "digest",
        help="Push daily digest (new trials + field changes) to CT_NOTIFY_* channels",
    )
    p_digest.add_argument("--dry-run", dest="dry_run", action="store_true",
                          help="Print the digest without sending or advancing the watermark")
    p_digest.add_argument("--english", action="store_true",
                          help="English digest text (default: Chinese)")
    p_digest.add_argument("--limit", type=int, default=500, help="Max change events per run")
    p_digest.set_defaults(func=cmd_digest)

    p_rq = sub.add_parser("review_queue", help="Manage the resolution review queue")
    p_rq_sub = p_rq.add_subparsers(dest="rq_action", required=True)

    p_rq_list = p_rq_sub.add_parser("list", help="List queue items")
    p_rq_list.add_argument("--status", dest="rq_status", default="pending",
                           choices=["pending", "approved", "rejected", "ignored"],
                           help="Filter by status (default: pending)")
    p_rq_list.set_defaults(func=cmd_review_queue)

    p_rq_approve = p_rq_sub.add_parser("approve", help="Approve a queued match")
    p_rq_approve.add_argument("--id", dest="rq_id", type=int, required=True,
                              help="Queue entry ID")
    p_rq_approve.add_argument("--reviewer", dest="rq_reviewer", default=None,
                              help="Reviewer identity (name or email)")
    p_rq_approve.set_defaults(func=cmd_review_queue)

    p_rq_reject = p_rq_sub.add_parser("reject", help="Reject a queued match")
    p_rq_reject.add_argument("--id", dest="rq_id", type=int, required=True,
                             help="Queue entry ID")
    p_rq_reject.add_argument("--reviewer", dest="rq_reviewer", default=None,
                             help="Reviewer identity (name or email)")
    p_rq_reject.set_defaults(func=cmd_review_queue)

    p_rq_stats = p_rq_sub.add_parser(
        "stats", help="Show pending queue counts by confidence bucket")
    p_rq_stats.set_defaults(func=cmd_review_queue)

    p_rq_batch = p_rq_sub.add_parser(
        "batch", help="Batch approve/reject by confidence (dry-run by default)")
    p_rq_batch.add_argument("--min-confidence", dest="rq_min_conf", type=float,
                            default=None,
                            help="Only items with confidence >= this value")
    p_rq_batch.add_argument("--max-confidence", dest="rq_max_conf", type=float,
                            default=None,
                            help="Only items with confidence <= this value")
    p_rq_batch.add_argument("--limit", dest="rq_limit", type=int, default=None,
                            help="Cap the number of items in this batch")
    p_rq_batch.add_argument("--action", dest="rq_batch_action",
                            default="approve", choices=["approve", "reject"],
                            help="What to do with the selected items (default: approve)")
    p_rq_batch.add_argument("--reviewer", dest="rq_reviewer", default=None,
                            help="Reviewer identity (name or email)")
    p_rq_batch.add_argument("--yes", dest="rq_yes", action="store_true",
                            help="Actually write to the database "
                                 "(default: dry-run, only prints what would change)")
    p_rq_batch.set_defaults(func=cmd_review_queue)

    p_rq_export = p_rq_sub.add_parser("export", help="Export queue items to CSV")
    p_rq_export.add_argument("--output", dest="rq_output", required=True,
                             help="Output CSV path (utf-8-sig, Excel friendly)")
    p_rq_export.add_argument("--min-confidence", dest="rq_export_min_conf",
                             type=float, default=None,
                             help="Only items with confidence >= this value")
    p_rq_export.add_argument("--status", dest="rq_export_status",
                             default="pending",
                             choices=["pending", "approved", "rejected", "ignored"],
                             help="Filter by status (default: pending)")
    p_rq_export.set_defaults(func=cmd_review_queue)

    p_stats = sub.add_parser("stats", help="Show database stats")
    p_stats.set_defaults(func=cmd_stats)

    p_backup = sub.add_parser(
        "backup", help="Back up the SQLite database (online backup API, WAL-safe)")
    p_backup.add_argument("--out", default=None,
                          help="Output directory (default: project backups/ dir)")
    p_backup.add_argument("--keep", type=int, default=7,
                          help="How many backups to retain per label (default: 7)")
    p_backup.add_argument("--label", default="db",
                          help="Filename label, backups keep the newest N per label "
                               "(default: db)")
    p_backup.set_defaults(func=cmd_backup)

    p_audit = sub.add_parser("integrity-check", help="Read-only SQLite and logical integrity audit")
    p_audit.add_argument("--db", help="Database path (default: configured DB)")
    p_audit.add_argument("--json", action="store_true", help="Machine-readable result")
    p_audit.set_defaults(func=cmd_integrity_check)

    p_restore = sub.add_parser("restore", help="Restore a verified backup while all writers are stopped")
    p_restore.add_argument("--input", required=True, help="Backup database file")
    p_restore.add_argument("--db", help="Target database path (default: configured DB)")
    p_restore.add_argument("--offline", action="store_true", help="Confirm server and all writers are stopped")
    p_restore.set_defaults(func=cmd_restore)

    p_diag = sub.add_parser("diagnostics", help="Local schema, size and row counts")
    p_diag.add_argument("--db", help="Database path (default: configured DB)")
    p_diag.set_defaults(func=cmd_diagnostics)

    p_recover = sub.add_parser("recover-stale", help="Mark old worker claims interrupted")
    p_recover.add_argument("--minutes", type=int, default=120, help="Age threshold (default: 120)")
    p_recover.set_defaults(func=cmd_recover_stale)

    p_tick = sub.add_parser("tick-monitors", help="Run topic monitors whose scheduled occurrence is due (scheduler tick; launchd every 15 min)")
    p_tick.set_defaults(func=cmd_tick_monitors)

    p_pipe = sub.add_parser("pipeline", help="One-command flow: crawl -> resolve -> quality -> export -> report -> backup")
    p_pipe.add_argument("--profile", default="mi",
                        help="Single disease profile key (default: mi)")
    p_pipe.add_argument("--profiles", default=None,
                        help="Multi-project mode: comma-separated profile keys "
                             "(e.g. mi,hf,cmp,mm) or 'all' — one combined crawl, "
                             "then per-profile reports")
    p_pipe.add_argument("--source", default="clinicaltrials_gov",
                        help="Source to crawl incrementally (default: clinicaltrials_gov; "
                             "ChiCTR/CTR are WAF-sensitive and best run manually)")
    p_pipe.add_argument("--english", action="store_true",
                        help="Explicitly request English reports for the report step "
                             "(English is the default; only needed to make the choice "
                             "explicit or to override --chinese in shared templates)")
    p_pipe.add_argument("--chinese", action="store_true",
                        help="Force Chinese reports for the report step (runs the LLM "
                             "translation pass; needs DEEPSEEK_API_KEY, else dictionary "
                             "fallback). Default: English.")
    p_pipe.add_argument("--full", action="store_true", help="Full report rebuild instead of incremental")
    p_pipe.add_argument("--skip-crawl", action="store_true", help="Skip the crawl step")
    p_pipe.add_argument("--skip-quality", action="store_true",
                        help="Skip the R4 quality-check step (runs after resolve by default)")
    p_pipe.add_argument("--skip-backup", action="store_true",
                        help="Skip the database backup step (runs after reports by default)")
    p_pipe.add_argument("--skip-digest", dest="skip_digest", action="store_true",
                        help="Skip the daily digest push step (runs after reports by default; "
                             "no-op success when no CT_NOTIFY_* channel is configured)")
    p_pipe.add_argument("--notify-success", action="store_true",
                        help="Also send an info-level brief when every step succeeds "
                             "(failure alerts are always sent when channels are configured)")
    p_pipe.set_defaults(func=cmd_pipeline)

    args = parser.parse_args()
    if args.command not in {"version", "doctor", "status", "integrity-check", "diagnostics"}:
        ensure_dirs()
        _init_logging()

    try:
        args.func(args)
    except NotImplementedError as e:
        print(f"Not implemented: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception("Command failed")
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        close_connection()


if __name__ == "__main__":
    main()
