"""
Database schema — all CREATE TABLE statements.

Migration strategy: additive where possible.  v3 introduces a breaking change
(master_trials PK from INTEGER to TEXT UUID) handled via table rename + recreate.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid as _uuid

from db.connection import get_connection

logger = logging.getLogger(__name__)

# ── Lookup / Metadata tables (loaded once, rarely mutated) ────────────────

SQL_CREATE_REGISTRY_SOURCES = """
CREATE TABLE IF NOT EXISTS registry_sources (
    source_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    short_name       TEXT    NOT NULL UNIQUE,     -- e.g. 'NCT', 'ChiCTR', 'CTR', 'ICTRP'
    full_name        TEXT    NOT NULL,
    base_url         TEXT,
    api_base         TEXT,
    enabled          INTEGER NOT NULL DEFAULT 0,  -- boolean
    source_type      TEXT    NOT NULL DEFAULT 'PRIMARY',  -- 'PRIMARY' or 'AGGREGATOR'
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

SQL_CREATE_STUDY_TYPES = """
CREATE TABLE IF NOT EXISTS study_types (
    study_type_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT    NOT NULL UNIQUE       -- 'Interventional', 'Observational', etc.
);
"""

SQL_CREATE_STATUS_TYPES = """
CREATE TABLE IF NOT EXISTS status_types (
    status_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT    NOT NULL UNIQUE       -- 'Recruiting', 'Completed', 'Not yet recruiting', etc.
);
"""

# ── Core data tables ─────────────────────────────────────────────────────

SQL_CREATE_REGISTRY_RECORDS = """
CREATE TABLE IF NOT EXISTS registry_records (
    record_id             INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Source identity
    source_id             INTEGER NOT NULL REFERENCES registry_sources(source_id),
    source_trial_id       TEXT    NOT NULL,           -- NCT12345678, ChiCTR2000xxxx, CTR2025xxxx …

    -- Pointers
    source_url            TEXT,
    title                 TEXT,
    scientific_title      TEXT,
    study_type_id         INTEGER REFERENCES study_types(study_type_id),
    status_id             INTEGER REFERENCES status_types(status_type_id),

    -- Enrolment & dates
    enrollment            INTEGER,                    -- raw enrolment number
    registration_date     TEXT,                       -- date first registered at source
    start_date            TEXT,
    primary_completion_date TEXT,
    completion_date       TEXT,
    last_updated_at_source TEXT,                      -- last-modified timestamp from source

    -- Structured data (keep the full original + normalised extracts)
    conditions            TEXT,                       -- JSON array or semicolon-separated
    interventions         TEXT,                       -- JSON array
    countries             TEXT,                       -- JSON array
    locations             TEXT,                       -- JSON array of site objects
    sponsors              TEXT,                       -- JSON array
    study_phase           TEXT,
    study_design          TEXT,
    eligibility_criteria  TEXT,
    primary_endpoint      TEXT,
    secondary_endpoints   TEXT,
    arm_group_interventions TEXT,

    -- Raw payload
    raw_payload           TEXT,                       -- Full JSON from the source API

    -- Hash (byte-level fingerprint of normalised content for change detection)
    data_hash             TEXT,

    -- Bootstrap flag: 1 = imported during initial bootstrap (excluded from "new" counts)
    is_bootstrap          INTEGER NOT NULL DEFAULT 0,

    -- Housekeeping
    first_crawled_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_crawled_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    is_latest             INTEGER NOT NULL DEFAULT 1, -- 1 = most recent version of this record

    -- Version chain
    superseded_by         INTEGER REFERENCES registry_records(record_id),
    version_number        INTEGER NOT NULL DEFAULT 1,

    UNIQUE(source_id, source_trial_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_records_source ON registry_records(source_id, source_trial_id);
-- Sibling joins (bilingual glossary mining, cross-source dedup) match on
-- source_trial_id across DIFFERENT sources, so the composite above (leading
-- source_id) cannot serve them.  Without this index the join degrades to a
-- quadratic scan and blocks every glossary-miss search for minutes.
CREATE INDEX IF NOT EXISTS idx_records_sibling ON registry_records(source_trial_id);
CREATE INDEX IF NOT EXISTS idx_records_hash   ON registry_records(data_hash);
CREATE INDEX IF NOT EXISTS idx_records_latest ON registry_records(is_latest);
CREATE INDEX IF NOT EXISTS idx_records_updated ON registry_records(last_updated_at_source);
CREATE INDEX IF NOT EXISTS idx_records_bootstrap ON registry_records(is_bootstrap);
"""

# ── Sync Status (last_successful_sync vs last_attempted_sync) ────────────

SQL_CREATE_SYNC_STATUS = """
CREATE TABLE IF NOT EXISTS sync_status (
    source_id            INTEGER PRIMARY KEY REFERENCES registry_sources(source_id),
    last_attempted_sync  TEXT,                   -- when we last TRIED to sync
    last_successful_sync TEXT,                   -- when we last SUCCESSFULLY synced
    bootstrap_completed  INTEGER NOT NULL DEFAULT 0,
    last_record_count    INTEGER DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ── Durable registry ingestion operations (v16) ─────────────────────────

SQL_CREATE_REGISTRY_INGESTION_RUNS = """
CREATE TABLE IF NOT EXISTS registry_ingestion_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES registry_sources(source_id),
    trigger TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    cursor_before TEXT,
    cursor_after TEXT,
    records_requested INTEGER,
    records_fetched INTEGER,
    records_new INTEGER,
    records_updated INTEGER,
    records_unchanged INTEGER,
    records_failed INTEGER,
    trial_events_created INTEGER,
    failure_stage TEXT,
    error_type TEXT,
    error_message TEXT,
    retry_of_run_id INTEGER REFERENCES registry_ingestion_runs(run_id),
    -- Set only for a scheduler occurrence.  It makes repeated ticks and
    -- process restarts idempotent without affecting manual runs.
    scheduled_for TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_time ON registry_ingestion_runs(source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_running ON registry_ingestion_runs(source_id, status) WHERE status='running';
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_runs_scheduled_occurrence
    ON registry_ingestion_runs(source_id, scheduled_for) WHERE scheduled_for IS NOT NULL;
"""

SQL_CREATE_REGISTRY_INGESTION_FAILURES = """
CREATE TABLE IF NOT EXISTS registry_ingestion_failures (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES registry_ingestion_runs(run_id),
    source_trial_id TEXT,
    stage TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ingestion_failures_run ON registry_ingestion_failures(run_id);
"""

# ── Entity Resolution v3 (UUID-based) ─────────────────────────────────────

SQL_CREATE_MASTER_TRIALS_V3 = """
CREATE TABLE IF NOT EXISTS master_trials (
    master_trial_id       TEXT    PRIMARY KEY,       -- UUID v4
    preferred_title       TEXT,
    scientific_title      TEXT,
    study_type_id         INTEGER REFERENCES study_types(study_type_id),
    status_id             INTEGER REFERENCES status_types(status_type_id),
    conditions            TEXT,
    interventions         TEXT,
    enrollment            INTEGER,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

SQL_CREATE_TRIAL_IDENTIFIERS = """
CREATE TABLE IF NOT EXISTS trial_identifiers (
    identifier_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id            INTEGER NOT NULL REFERENCES registry_records(record_id),
    identifier_type      TEXT    NOT NULL,         -- 'NCT', 'CTR', 'ChiCTR', 'UTN', 'ProtocolNumber'
    identifier_value     TEXT    NOT NULL,
    source_field         TEXT,                     -- 'source_trial_id', 'secondary_id', 'raw_payload', etc.
    confidence           REAL    NOT NULL DEFAULT 1.0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(record_id, identifier_type, identifier_value)
);

CREATE INDEX IF NOT EXISTS idx_identifiers_type_value ON trial_identifiers(identifier_type, identifier_value);
CREATE INDEX IF NOT EXISTS idx_identifiers_record     ON trial_identifiers(record_id);
"""

SQL_CREATE_RECORD_MASTER_MAP_V3 = """
CREATE TABLE IF NOT EXISTS record_master_map (
    map_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id             INTEGER NOT NULL REFERENCES registry_records(record_id),
    master_trial_id       TEXT    NOT NULL REFERENCES master_trials(master_trial_id),
    match_method          TEXT    NOT NULL,
    match_confidence      REAL    NOT NULL DEFAULT 1.0,
    match_status          TEXT    NOT NULL DEFAULT 'AUTO_CONFIRMED',  -- 'AUTO_CONFIRMED', 'POSSIBLE', 'REJECTED'
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(record_id, master_trial_id)
);

CREATE INDEX IF NOT EXISTS idx_map2_record  ON record_master_map(record_id);
CREATE INDEX IF NOT EXISTS idx_map2_master  ON record_master_map(master_trial_id);
"""

SQL_CREATE_RESOLUTION_QUEUE = """
CREATE TABLE IF NOT EXISTS resolution_queue (
    queue_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id             INTEGER NOT NULL REFERENCES registry_records(record_id),
    suggested_master_id   TEXT    REFERENCES master_trials(master_trial_id),
    match_method          TEXT    NOT NULL,
    confidence            REAL    NOT NULL,
    reasoning             TEXT,
    status                TEXT    NOT NULL DEFAULT 'pending',  -- 'pending', 'approved', 'rejected', 'ignored'
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    reviewed_at           TEXT,
    reviewed_by           TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status    ON resolution_queue(status);
CREATE INDEX IF NOT EXISTS idx_queue_record    ON resolution_queue(record_id);
"""

# ── Change tracking ──────────────────────────────────────────────────────

SQL_CREATE_CHANGE_EVENTS = """
CREATE TABLE IF NOT EXISTS change_events (
    event_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id             INTEGER NOT NULL REFERENCES registry_records(record_id),
    master_trial_id       TEXT    REFERENCES master_trials(master_trial_id),

    field_name            TEXT    NOT NULL,
    old_value             TEXT,
    new_value             TEXT,
    change_category       TEXT,                   -- 'status', 'enrollment', 'endpoint', 'date', 'location', 'sponsor', 'design', 'other'

    detected_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    acknowledged          INTEGER NOT NULL DEFAULT 0,
    acknowledged_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_change_record   ON change_events(record_id);
CREATE INDEX IF NOT EXISTS idx_change_ack      ON change_events(acknowledged);
CREATE INDEX IF NOT EXISTS idx_change_detected ON change_events(detected_at);
"""

# ── Trial Events (enhanced change events with event_hash dedup) ────────────

SQL_CREATE_TRIAL_EVENTS = """
CREATE TABLE IF NOT EXISTS trial_events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id         INTEGER NOT NULL REFERENCES registry_records(record_id),
    master_trial_id   TEXT    REFERENCES master_trials(master_trial_id),
    event_type        TEXT NOT NULL DEFAULT 'field_change',
    field_name        TEXT NOT NULL,
    old_value         TEXT,
    new_value         TEXT,
    change_category   TEXT,
    event_hash        TEXT NOT NULL UNIQUE,       -- SHA-256 dedup fingerprint
    detected_at       TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged      INTEGER NOT NULL DEFAULT 0,
    acknowledged_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_trial_events_hash   ON trial_events(event_hash);
CREATE INDEX IF NOT EXISTS idx_trial_events_record ON trial_events(record_id);
CREATE INDEX IF NOT EXISTS idx_trial_events_ack    ON trial_events(acknowledged);
-- Window scans (_event_query) filter on detected_at; the planner needs this
-- to start from the (small) event side instead of scanning registry_records.
CREATE INDEX IF NOT EXISTS idx_trial_events_detected ON trial_events(detected_at);
"""

# ── Individual trial monitoring (v11) ───────────────────────────────────
# Deliberately local, single-user state.  ``trial_id`` is the canonical
# registry identifier used by the existing event/version queries; no fake
# user/workspace row is needed before multi-user support exists.
SQL_CREATE_TRIAL_WATCHES = """
CREATE TABLE IF NOT EXISTS trial_watches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id            TEXT NOT NULL UNIQUE,
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_viewed_at      TEXT,
    last_change_seen_at TEXT,
    label               TEXT,
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_trial_watches_enabled ON trial_watches(enabled, updated_at);
"""

SQL_CREATE_WATCH_PREFERENCES = """
CREATE TABLE IF NOT EXISTS watch_preferences (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_watch_id  INTEGER NOT NULL REFERENCES trial_watches(id),
    field_group     TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    minimum_severity TEXT NOT NULL DEFAULT 'normal',
    UNIQUE(trial_watch_id, field_group)
);
CREATE INDEX IF NOT EXISTS idx_watch_preferences_watch ON watch_preferences(trial_watch_id);
"""

# ── Topic monitors (v12) ────────────────────────────────────────────────
SQL_CREATE_MONITORS = """
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    monitor_type TEXT NOT NULL DEFAULT 'topic', enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')), last_checked_at TEXT,
    schedule_enabled INTEGER NOT NULL DEFAULT 0, schedule_frequency TEXT,
    schedule_timezone TEXT NOT NULL DEFAULT 'UTC', next_run_at TEXT, last_scheduled_run_at TEXT
);
CREATE TABLE IF NOT EXISTS monitor_rules (
    monitor_id INTEGER PRIMARY KEY REFERENCES monitors(id), rules_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS monitor_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL REFERENCES monitors(id), trial_id TEXT NOT NULL,
    first_matched_at TEXT NOT NULL DEFAULT (datetime('now')), last_matched_at TEXT,
    currently_matches INTEGER NOT NULL DEFAULT 1, entered_at TEXT, left_at TEXT, last_evaluated_at TEXT,
    UNIQUE(monitor_id, trial_id)
);
CREATE INDEX IF NOT EXISTS idx_monitor_trials_current ON monitor_trials(monitor_id, currently_matches);
-- Scope filters probe membership by trial id (EXISTS (... mt.trial_id=r.source_trial_id));
-- without this index every probe full-scans monitor_trials.
CREATE INDEX IF NOT EXISTS idx_monitor_trials_trial ON monitor_trials(trial_id, currently_matches);
CREATE TABLE IF NOT EXISTS monitor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL REFERENCES monitors(id),
    started_at TEXT NOT NULL DEFAULT (datetime('now')), completed_at TEXT, status TEXT NOT NULL DEFAULT 'running',
    matched_count INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0, changed_count INTEGER NOT NULL DEFAULT 0,
    left_count INTEGER NOT NULL DEFAULT 0, error_message TEXT,
    trigger TEXT NOT NULL DEFAULT 'manual', scheduled_for TEXT,
    UNIQUE(monitor_id, scheduled_for)
);
CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL REFERENCES monitors(id), trial_id TEXT NOT NULL,
    event_type TEXT NOT NULL, related_change_event_id INTEGER REFERENCES trial_events(event_id),
    detected_at TEXT NOT NULL DEFAULT (datetime('now')), metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_events_monitor ON monitor_events(monitor_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_events_trial ON monitor_events(trial_id);
"""

# Passive query library (v17). No monitor/scheduler/notification foreign keys.
SQL_CREATE_SAVED_SEARCHES = """
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    search_schema_version INTEGER NOT NULL DEFAULT 1,
    state_json TEXT NOT NULL,
    original_nl_query TEXT,
    interpreter_version TEXT,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_opened_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_saved_searches_order
    ON saved_searches(pinned DESC, last_opened_at DESC, updated_at DESC);
"""

# Learned bilingual glossary (v19) — mined from search result sets
# (same-record zh/en field pairs in ChiCTR-style data). Expansion lives in
# the search endpoints only; saved monitors keep their saved semantics.
SQL_CREATE_GLOSSARY_TERMS = """
CREATE TABLE IF NOT EXISTS glossary_terms (
    term_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    zh_term     TEXT NOT NULL,
    en_term     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'learned',    -- 'learned' | 'manual'
    status      TEXT NOT NULL DEFAULT 'candidate',  -- 'candidate' | 'active' | 'retired'
    evidence    INTEGER NOT NULL DEFAULT 0,         -- distinct supporting records
    hits        INTEGER NOT NULL DEFAULT 0,         -- times the pair served an expansion
    example_record_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array, audit trail
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,
    UNIQUE(zh_term, en_term)
);
CREATE INDEX IF NOT EXISTS idx_glossary_terms_status ON glossary_terms(status);
"""

# Single-user research workspaces (v18). Associations never own the linked asset.
SQL_CREATE_RESEARCH_PROJECTS = """
CREATE TABLE IF NOT EXISTS research_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS project_saved_searches (
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    saved_search_id INTEGER NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, saved_search_id)
);
CREATE INDEX IF NOT EXISTS idx_project_saved_search_asset ON project_saved_searches(saved_search_id);
CREATE TABLE IF NOT EXISTS project_monitors (
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, monitor_id)
);
CREATE INDEX IF NOT EXISTS idx_project_monitor_asset ON project_monitors(monitor_id);
CREATE TABLE IF NOT EXISTS project_trials (
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, source, trial_id)
);
CREATE INDEX IF NOT EXISTS idx_project_trial_asset ON project_trials(source, trial_id);
CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_project_notes_project ON project_notes(project_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS project_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES trial_events(event_id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_project_evidence_event ON project_evidence(event_id);
"""

# ── In-app notification outbox (v14) ────────────────────────────────────
SQL_CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS notification_settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER, monitor_event_id INTEGER NOT NULL,
    trial_id TEXT, event_type TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'in_app',
    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')), delivered_at TEXT, read_at TEXT,
    summary TEXT, monitor_name_snapshot TEXT, recipient TEXT, trial_title_snapshot TEXT,
    claim_token TEXT, claimed_at TEXT, UNIQUE(monitor_event_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_notifications_feed ON notifications(read_at, created_at DESC);
CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER NOT NULL REFERENCES notifications(id),
    attempt_number INTEGER NOT NULL, started_at TEXT NOT NULL DEFAULT (datetime('now')), finished_at TEXT,
    status TEXT NOT NULL, error_message TEXT
    , error_type TEXT, provider_name TEXT, provider_message_id TEXT
);
"""

# ── Snapshots & Audit ────────────────────────────────────────────────────

SQL_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id             INTEGER NOT NULL REFERENCES registry_sources(source_id),
    snapshot_type         TEXT    NOT NULL DEFAULT 'daily',  -- 'daily', 'weekly', 'baseline'
    snapshot_date         TEXT    NOT NULL,
    record_count          INTEGER NOT NULL,
    snapshot_data         TEXT,                   -- JSON: full dump of current records
    created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_source ON snapshots(source_id, snapshot_date);
"""

SQL_CREATE_CRAWL_LOG = """
CREATE TABLE IF NOT EXISTS crawl_log (
    log_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id             INTEGER NOT NULL REFERENCES registry_sources(source_id),
    started_at            TEXT    NOT NULL,
    completed_at          TEXT,
    records_found         INTEGER DEFAULT 0,
    records_new           INTEGER DEFAULT 0,
    records_updated       INTEGER DEFAULT 0,
    errors                TEXT,                   -- JSON array of error messages
    status                TEXT    NOT NULL DEFAULT 'running',  -- 'running', 'completed', 'failed'
    error_message         TEXT
);
"""

SQL_CREATE_REPORT_STATUS = """
CREATE TABLE IF NOT EXISTS report_status (
    report_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type           TEXT    NOT NULL,       -- 'daily', 'weekly', 'monthly'
    report_date           TEXT    NOT NULL,
    generated_at          TEXT    NOT NULL DEFAULT (datetime('now')),

    n_new_records         INTEGER DEFAULT 0,
    n_updated_records     INTEGER DEFAULT 0,
    n_new_master_trials   INTEGER DEFAULT 0,
    n_change_events       INTEGER DEFAULT 0,
    n_acknowledged        INTEGER DEFAULT 0,

    summary               TEXT,                   -- human-readable JSON summary
    UNIQUE(report_type, report_date)
);
"""

# ── MI report registry (incremental baseline for ct_report) ───────────────
# Owned here so the schema is defined in one place; ct_report.diffing
# calls ensure_report_registry() instead of creating its own table.
SQL_CREATE_REPORT_REGISTRY = """
CREATE TABLE IF NOT EXISTS report_registry (
    report_key      TEXT PRIMARY KEY,          -- e.g. 'mi_cross_source'
    generated_at    TEXT NOT NULL,
    report_file     TEXT NOT NULL,             -- path to generated HTML
    trial_ids_hash  TEXT,                      -- SHA256 of all source_trial_ids in report
    record_count    INTEGER DEFAULT 0,
    params          TEXT,                      -- JSON of query parameters
    trial_ids_json  TEXT                       -- full id+signature set for incremental diff
);
"""

# ── Provenance tracking (Phase 6: AGGREGATOR sources) ──────────────────────

SQL_CREATE_TRIAL_PROVENANCE = """
CREATE TABLE IF NOT EXISTS trial_provenance (
    provenance_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id         INTEGER NOT NULL REFERENCES registry_records(record_id),
    source_id         INTEGER NOT NULL REFERENCES registry_sources(source_id),
    field_name        TEXT    NOT NULL,
    field_value       TEXT,
    provenance_source TEXT    NOT NULL DEFAULT 'direct',  -- 'direct' (from primary source) or 'aggregator'
    is_conflict       INTEGER NOT NULL DEFAULT 0,         -- 1 = conflicts with value from another source
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(record_id, source_id, field_name)
);

CREATE INDEX IF NOT EXISTS idx_provenance_record ON trial_provenance(record_id);
CREATE INDEX IF NOT EXISTS idx_provenance_source ON trial_provenance(source_id);
"""

SQL_CREATE_CONFLICT_LOG = """
CREATE TABLE IF NOT EXISTS conflict_log (
    conflict_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    master_trial_id   TEXT    NOT NULL REFERENCES master_trials(master_trial_id),
    field_name        TEXT    NOT NULL,
    source_a_id       INTEGER NOT NULL REFERENCES registry_sources(source_id),
    source_b_id       INTEGER NOT NULL REFERENCES registry_sources(source_id),
    value_a           TEXT,
    value_b           TEXT,
    resolution        TEXT    NOT NULL DEFAULT 'unresolved',  -- 'unresolved', 'prefer_a', 'prefer_b', 'merged'
    resolved_at       TEXT,
    resolved_by       TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_conflict_master ON conflict_log(master_trial_id);
CREATE INDEX IF NOT EXISTS idx_conflict_status ON conflict_log(resolution);
"""

# ── Schema metadata ───────────────────────────────────────────────────────

SQL_CREATE_DISCOVERY_CURSORS = """
-- v7: per-source, per-mode incremental watermark for the discovery layer
-- (list_walk / id_probe / ictrp_diff / keyword_fallback). cursor_json
-- carries watermark_time, watermark_ids, recent_seen_ids,
-- last_complete_page and the ordering probe result (see
-- the project design notes §2.3, adopting the
-- cursor schema from the project design notes §4.3).
CREATE TABLE IF NOT EXISTS discovery_cursors (
    cursor_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES registry_sources(source_id),
    mode             TEXT    NOT NULL,
    cursor_json      TEXT    NOT NULL DEFAULT '{}',
    last_attempted   TEXT,
    last_successful  TEXT,
    status           TEXT    NOT NULL DEFAULT 'active',
    UNIQUE(source_id, mode)
);
"""

SQL_CREATE_DISCOVERY_QUEUE = """
-- v7: decoupled discovery → enrichment work queue. Discovery (cheap:
-- list pages, snapshot diffs, ID probes) writes pending rows; the
-- enrichment step (expensive: one WAF-challenge page load per detail)
-- consumes them at a bounded rate and is resumable/idempotent.
-- title: list-page title captured at discovery time — enables keyword-
-- prioritized (targeted) enrichment instead of blind newest-first
-- spending of the WAF budget. Nullable: ictrp_diff rows have no title
-- until the same number is seen again on a list page (backfilled).
CREATE TABLE IF NOT EXISTS discovery_queue (
    discovery_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES registry_sources(source_id),
    source_trial_id  TEXT    NOT NULL,
    discovered_via   TEXT    NOT NULL,
    discovered_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    state            TEXT    NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    title            TEXT,
    UNIQUE(source_id, source_trial_id)
);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_state
    ON discovery_queue(state, source_id);
"""

# ── Watch keywords (v9: user-defined subscription searches) ───────────────
# Users register free-text keywords; the daily pipeline OR-joins them into
# the ClinicalTrials.gov crawl query (public API only — WAF sources stay on
# their discovery queues), and the web digest shows per-keyword recent hits.
SQL_CREATE_WATCH_KEYWORDS = """
CREATE TABLE IF NOT EXISTS watch_keywords (
    watch_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword              TEXT    NOT NULL UNIQUE,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

SQL_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version               INTEGER NOT NULL,
    applied_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    description           TEXT
);
"""

# ── Digest state (v8: daily push watermark) ───────────────────────────────
# Single-row watermark for the daily digest push.  trial_events.acknowledged
# is never set anywhere in the codebase, so the digest tracks what it has
# already pushed via the monotonically increasing event_id (robust against
# clock skew, unlike detected_at comparisons).
SQL_CREATE_DIGEST_STATE = """
CREATE TABLE IF NOT EXISTS digest_state (
    id                INTEGER PRIMARY KEY,
    last_event_id     INTEGER NOT NULL DEFAULT 0,
    last_run_at       TEXT
);
"""

# ── Refresh queue (v8: Chinese-source re-verification) ────────────────────
# Periodic re-visit of already-enriched ChiCTR/CTR records so field updates
# (e.g. enrollment) get detected.  Mirrors discovery_queue's resumable work
# queue semantics, but a trial is re-queued over its lifetime, so uniqueness
# only applies to concurrently-pending rows (partial unique index): done
# rows are kept as the "last refreshed at" history for scheduling.
SQL_CREATE_REFRESH_QUEUE = """
CREATE TABLE IF NOT EXISTS refresh_queue (
    refresh_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES registry_sources(source_id),
    source_trial_id  TEXT    NOT NULL,
    record_id        INTEGER NOT NULL,      -- is_latest row at queue time
    state            TEXT    NOT NULL DEFAULT 'pending',  -- pending/done/failed
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    queued_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    done_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refresh_queue_pending
    ON refresh_queue(source_id, source_trial_id) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_refresh_queue_state
    ON refresh_queue(state, source_id);
"""

# ── Full-text index (v6) ──────────────────────────────────────────────────
# External-content FTS5 over registry_records(title, conditions), used by
# entity resolution to pick title-similarity candidates and (optionally) by
# report queries.  trigram tokenizer supports CJK substring matching.
# registry_records rows are never UPDATEed in place (versioning inserts new
# rows) and never DELETEd, so an AFTER INSERT trigger keeps the index in sync.
SQL_CREATE_RECORDS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    title,
    conditions,
    content='registry_records',
    content_rowid='record_id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS records_fts_insert AFTER INSERT ON registry_records BEGIN
    INSERT INTO records_fts(rowid, title, conditions)
    VALUES (new.record_id, new.title, new.conditions);
END;
"""

# ── All DDL ───────────────────────────────────────────────────────────────

ALL_TABLES: list[tuple[str, str]] = [
    ("registry_sources",    SQL_CREATE_REGISTRY_SOURCES),
    ("study_types",         SQL_CREATE_STUDY_TYPES),
    ("status_types",        SQL_CREATE_STATUS_TYPES),
    ("registry_records",    SQL_CREATE_REGISTRY_RECORDS),
    ("sync_status",         SQL_CREATE_SYNC_STATUS),
    ("registry_ingestion_runs", SQL_CREATE_REGISTRY_INGESTION_RUNS),
    ("registry_ingestion_failures", SQL_CREATE_REGISTRY_INGESTION_FAILURES),
    ("master_trials",       SQL_CREATE_MASTER_TRIALS_V3),
    ("trial_identifiers",   SQL_CREATE_TRIAL_IDENTIFIERS),
    ("record_master_map",   SQL_CREATE_RECORD_MASTER_MAP_V3),
    ("resolution_queue",    SQL_CREATE_RESOLUTION_QUEUE),
    ("change_events",       SQL_CREATE_CHANGE_EVENTS),
    ("trial_events",        SQL_CREATE_TRIAL_EVENTS),
    ("trial_watches",       SQL_CREATE_TRIAL_WATCHES),
    ("watch_preferences",   SQL_CREATE_WATCH_PREFERENCES),
    ("monitors",            SQL_CREATE_MONITORS),
    ("saved_searches",      SQL_CREATE_SAVED_SEARCHES),
    ("glossary_terms",      SQL_CREATE_GLOSSARY_TERMS),
    ("research_projects",  SQL_CREATE_RESEARCH_PROJECTS),
    ("notifications",       SQL_CREATE_NOTIFICATIONS),
    ("snapshots",           SQL_CREATE_SNAPSHOTS),
    ("crawl_log",           SQL_CREATE_CRAWL_LOG),
    ("report_status",       SQL_CREATE_REPORT_STATUS),
    ("report_registry",     SQL_CREATE_REPORT_REGISTRY),
    ("trial_provenance",    SQL_CREATE_TRIAL_PROVENANCE),
    ("conflict_log",        SQL_CREATE_CONFLICT_LOG),
    ("discovery_cursors",   SQL_CREATE_DISCOVERY_CURSORS),
    ("discovery_queue",     SQL_CREATE_DISCOVERY_QUEUE),
    ("digest_state",        SQL_CREATE_DIGEST_STATE),
    ("refresh_queue",       SQL_CREATE_REFRESH_QUEUE),
    ("watch_keywords",      SQL_CREATE_WATCH_KEYWORDS),
    ("schema_version",      SQL_CREATE_SCHEMA_VERSION),
]


def _column_exists(conn, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())


def _table_exists(conn, table: str) -> bool:
    cur = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone()[0] > 0


def _is_integer_pk(conn, table: str) -> bool:
    """Check if the table has an INTEGER PRIMARY KEY (vs TEXT)."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    for row in cur.fetchall():
        if row["pk"] and row["type"].upper() == "INTEGER":
            return True
    return False


def _execute_ddl(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement DDL script without executescript's implicit COMMIT."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete schema DDL")


def create_schema() -> None:
    """Create or migrate the schema as one SQLite transaction."""
    conn = get_connection()
    # Legacy table rebuilds require FK enforcement off while the transaction
    # runs. SQLite only accepts this PRAGMA outside a transaction.
    conn.commit()
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _create_schema_uncommitted(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"schema migration left {len(violations)} foreign-key violations")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")


def _create_schema_uncommitted(conn: sqlite3.Connection) -> None:
    """Run all version steps without committing or changing FK enforcement."""
    # Refuse a future/partial schema before CREATE IF NOT EXISTS can mutate it.
    if _table_exists(conn, "schema_version"):
        row = conn.execute("SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1").fetchone()
        if row and row["version"] > 20:
            raise RuntimeError(f"unsupported schema version {row['version']}; this build supports v20")

    # ── Phase 1 & 2 tables ────────────────────────────────────────────
    for table_name, ddl in ALL_TABLES:
        _execute_ddl(conn, ddl)

    # ── v7 upgrade guard: discovery_queue.title added after v7 first
    # shipped (list-page title enables keyword-prioritized enrichment) ──
    if not _column_exists(conn, "discovery_queue", "title"):
        conn.execute("ALTER TABLE discovery_queue ADD COLUMN title TEXT")

    # ── Phase 2 migration (is_bootstrap column) ───────────────────────
    if not _column_exists(conn, "registry_records", "is_bootstrap"):
        conn.execute(
            "ALTER TABLE registry_records ADD COLUMN is_bootstrap INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_bootstrap ON registry_records(is_bootstrap)"
        )

    # ── Ensure source_type column exists BEFORE seed_lookups ──────────
    if not _column_exists(conn, "registry_sources", "source_type"):
        conn.execute(
            "ALTER TABLE registry_sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'PRIMARY'"
        )

    # Seed lookup data
    _seed_lookups(conn)

    # ── Schema version tracking ───────────────────────────────────────
    cur = conn.execute("SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1")
    row = cur.fetchone()
    current_version = row["version"] if row else 0

    if current_version < 1:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (1, "Initial schema: sources, records, master trials, change events, snapshots, reports"),
        )
        current_version = 1

    if current_version < 2:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (2, "Phase 2: sync_status, trial_events, is_bootstrap column"),
        )
        current_version = 2

    if current_version < 3:
        _migrate_v3(conn)

    # ── Phase 4 migration (AGGREGATOR source_type) ────────────────────────
    if current_version < 4:
        # Create Phase-6 tables (trial_provenance, conflict_log)
        for ddl in (SQL_CREATE_TRIAL_PROVENANCE, SQL_CREATE_CONFLICT_LOG):
            _execute_ddl(conn, ddl)
        # Mark ICTRP as AGGREGATOR
        conn.execute(
            "UPDATE registry_sources SET source_type = 'AGGREGATOR' WHERE short_name = 'ICTRP'"
        )
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (4, "Phase 6: AGGREGATOR source_type, trial_provenance, conflict_log"),
        )
        current_version = 4

    # ── v5 migration: data quality hardening ──────────────────────────
    if current_version < 5:
        _migrate_v5(conn)
        current_version = 5

    # ── v6 migration: FTS5 index for title/conditions ──────────────────
    if current_version < 6:
        if _ensure_records_fts(conn):
            # Backfill the index from existing rows (no-op on fresh DBs)
            conn.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
            description = "v6: records_fts (FTS5 trigram) for entity-resolution candidates"
        else:
            description = "v6: FTS5 unavailable in this SQLite build — index skipped"
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (6, description),
        )
        current_version = 6

    # ── v7 migration: discovery layer (cursors + work queue) ───────────
    # Tables themselves are CREATE IF NOT EXISTS via ALL_TABLES above, so
    # this block only records the version bump (idempotent).
    if current_version < 7:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (7, "v7: discovery_cursors + discovery_queue (decoupled discovery/enrichment)"),
        )
        current_version = 7

    # ── v8 migration: digest watermark + Chinese-source refresh queue ──
    # Same pattern as v7: DDL is CREATE IF NOT EXISTS via ALL_TABLES, this
    # only records the version bump (idempotent).
    if current_version < 8:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (8, "v8: digest_state (push watermark) + refresh_queue (Chinese-source re-verification)"),
        )
        current_version = 8

    # ── v9 migration: user watch keywords ─────────────────────────────
    # CREATE IF NOT EXISTS via ALL_TABLES above; this only records the bump.
    if current_version < 9:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (9, "v9: watch_keywords (user-defined subscription searches)"),
        )
        current_version = 9

    # ── v10 migration: change severity on trial_events ────────────────
    if current_version < 10:
        _migrate_v10(conn)
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (10, "v10: trial_events severity / change_type / importance_score "
                 "(central classifier core.severity) + backfill"),
        )
        current_version = 10

    # ── v11 migration: persisted individual-trial watches ─────────────
    # DDL is additive and has already run through ALL_TABLES.  The version
    # watermark keeps existing v10 databases readable and migration idempotent.
    if current_version < 11:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (11, "v11: trial_watches + watch_preferences for local individual trial monitoring"),
        )
        current_version = 11

    if current_version < 12:
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                     (12, "v12: topic monitors, rules, persistent membership, runs and activity"))
        current_version = 12

    if current_version < 13:
        for col, spec in (("schedule_enabled", "INTEGER NOT NULL DEFAULT 0"), ("schedule_frequency", "TEXT"),
                          ("schedule_timezone", "TEXT NOT NULL DEFAULT 'UTC'"), ("next_run_at", "TEXT"),
                          ("last_scheduled_run_at", "TEXT")):
            if not _column_exists(conn, "monitors", col): conn.execute(f"ALTER TABLE monitors ADD COLUMN {col} {spec}")
        for col, spec in (("trigger", "TEXT NOT NULL DEFAULT 'manual'"), ("scheduled_for", "TEXT")):
            if not _column_exists(conn, "monitor_runs", col): conn.execute(f"ALTER TABLE monitor_runs ADD COLUMN {col} {spec}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_runs_occurrence ON monitor_runs(monitor_id, scheduled_for) WHERE scheduled_for IS NOT NULL")
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (13, "v13: durable UTC topic-monitor scheduling and occurrence idempotency"))
        current_version = 13

    if current_version < 14:
        conn.execute("INSERT OR IGNORE INTO notification_settings (key,value) VALUES ('activation_at', datetime('now'))")
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (14, "v14: durable in-app notification outbox with post-migration cutover"))
        current_version = 14

    # A notification must remain intelligible if its monitor is later deleted.
    # This guard also upgrades databases created by an earlier v14 development
    # build before the display snapshot was introduced.
    if not _column_exists(conn, "notifications", "monitor_name_snapshot"):
        conn.execute("ALTER TABLE notifications ADD COLUMN monitor_name_snapshot TEXT")

    if current_version < 15:
        # Safe defaults: existing monitors do not begin emailing after upgrade.
        for col, spec in (("email_notifications_enabled", "INTEGER NOT NULL DEFAULT 0"),
                          ("email_recipient", "TEXT"), ("email_enabled_at", "TEXT")):
            if not _column_exists(conn, "monitors", col):
                conn.execute(f"ALTER TABLE monitors ADD COLUMN {col} {spec}")
        for col, spec in (("recipient", "TEXT"), ("trial_title_snapshot", "TEXT"),
                          ("claim_token", "TEXT"), ("claimed_at", "TEXT")):
            if not _column_exists(conn, "notifications", col):
                conn.execute(f"ALTER TABLE notifications ADD COLUMN {col} {spec}")
        for col, spec in (("error_type", "TEXT"), ("provider_name", "TEXT"),
                          ("provider_message_id", "TEXT")):
            if not _column_exists(conn, "notification_delivery_attempts", col):
                conn.execute(f"ALTER TABLE notification_delivery_attempts ADD COLUMN {col} {spec}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_email_recipient ON notifications(monitor_event_id, channel, recipient) WHERE channel='email'")
        conn.execute("INSERT OR IGNORE INTO notification_settings (key,value) VALUES ('email_activation_at', datetime('now'))")
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)", (15, "v15: per-monitor email notification outbox and delivery audit"))
        current_version = 15

    if current_version < 16:
        # Existing databases have no trustworthy historic run metrics.  Keep
        # their operational state Unknown until the first v16 run rather than
        # manufacturing a prior successful ingestion.
        for col, spec in (("source_current_through", "TEXT"),
                          ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
                          ("last_run_id", "INTEGER"),
                          ("next_sync_at", "TEXT")):
            if not _column_exists(conn, "sync_status", col):
                conn.execute(f"ALTER TABLE sync_status ADD COLUMN {col} {spec}")
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                     (16, "v16: durable registry ingestion runs, failure audit, and source freshness state"))
        current_version = 16

    if current_version < 17:
        _execute_ddl(conn, SQL_CREATE_SAVED_SEARCHES)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                     (17, "v17: passive, versioned saved-search query library"))
        current_version = 17

    if current_version < 18:
        _execute_ddl(conn, SQL_CREATE_RESEARCH_PROJECTS)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                     (18, "v18: local research projects, asset associations, notes and evidence"))
        current_version = 18

    if current_version < 19:
        _execute_ddl(conn, SQL_CREATE_GLOSSARY_TERMS)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                     (19, "v19: learned bilingual glossary mined from search result bilingual field pairs"))
        current_version = 19

    if current_version < 20:
        # Idempotent for databases whose base DDL already created the index.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_sibling "
                     "ON registry_records(source_trial_id)")
        # The planner otherwise cannot tell is_latest (7k rows per value)
        # from source_trial_id (near-unique) and regularly picks the wrong
        # index for the sibling join.  Targeted ANALYZE is milliseconds on
        # this table and the relative selectivities it records stay valid
        # as the table grows.
        conn.execute("ANALYZE registry_records")
        conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                     (20, "v20: registry_records(source_trial_id) index — cross-source "
                          "sibling join was a quadratic scan blocking glossary-miss searches"))
        current_version = 20

    # Development builds may have applied the v16 watermark before the
    # scheduler-occurrence guard was introduced.  Keep this idempotent for
    # both those databases and ordinary v15 upgrades.
    if not _column_exists(conn, "registry_ingestion_runs", "scheduled_for"):
        conn.execute("ALTER TABLE registry_ingestion_runs ADD COLUMN scheduled_for TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_runs_scheduled_occurrence "
                 "ON registry_ingestion_runs(source_id, scheduled_for) WHERE scheduled_for IS NOT NULL")

def _ensure_records_fts(conn) -> bool:
    """Create the records_fts virtual table + sync trigger.

    Returns True when FTS5 is available, False otherwise (the rest of the
    schema is unaffected; entity_resolution falls back to a plain scan).
    """
    try:
        conn.execute("SAVEPOINT records_fts_install")
        _execute_ddl(conn, SQL_CREATE_RECORDS_FTS)
        conn.execute("RELEASE records_fts_install")
        return True
    except Exception as exc:
        conn.execute("ROLLBACK TO records_fts_install")
        conn.execute("RELEASE records_fts_install")
        logger.warning("FTS5 not available, skipping records_fts: %s", exc)
        return False


def fts_available() -> bool:
    """Whether the records_fts index exists in the current database."""
    conn = get_connection()
    return _table_exists(conn, "records_fts")


def ensure_report_registry() -> None:
    """Guarantee the report_registry table exists with the trial_ids_json
    column (handles databases created before the column was added)."""
    conn = get_connection()
    conn.executescript(SQL_CREATE_REPORT_REGISTRY)
    if not _column_exists(conn, "report_registry", "trial_ids_json"):
        conn.execute("ALTER TABLE report_registry ADD COLUMN trial_ids_json TEXT")
    conn.commit()


def _migrate_v3(conn) -> None:
    """
    v3 migration: UUID-based master trials + trial_identifiers + resolution_queue.

    What happens:
      1. Rename old master_trials -> master_trials_legacy
      2. Rename old record_master_map -> record_master_map_legacy
      3. Create new master_trials (TEXT UUID PK)
      4. Create trial_identifiers, record_master_map (w/ match_status), resolution_queue
      5. Migrate existing data to new tables
      6. Clear master_trial_id in change_events / trial_events (legacy INTEGER refs)
    """
    logger.info("Running v3 migration (UUID master trials, trial_identifiers, resolution_queue) …")

    old_mt_exists = _table_exists(conn, "master_trials") and _is_integer_pk(conn, "master_trials")
    if not old_mt_exists:
        # Fresh install — the v3 DDL in ALL_TABLES already created the right tables.
        # Just ensure the new tables exist.
        for ddl in (SQL_CREATE_TRIAL_IDENTIFIERS, SQL_CREATE_RESOLUTION_QUEUE):
            _execute_ddl(conn, ddl)
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (3, "Phase 3: UUID master trials, trial_identifiers, resolution_queue, match_status"),
        )
        logger.info("v3 migration complete (fresh install).")
        return

    # ── Upgrade from v2 ──────────────────────────────────────────────

    # 1. Rename legacy tables
    conn.execute("ALTER TABLE master_trials RENAME TO master_trials_legacy")
    if _table_exists(conn, "record_master_map"):
        conn.execute("ALTER TABLE record_master_map RENAME TO record_master_map_legacy")

    # 2. Create v3 tables (they don't exist after rename)
    _execute_ddl(conn, SQL_CREATE_MASTER_TRIALS_V3)
    _execute_ddl(conn, SQL_CREATE_TRIAL_IDENTIFIERS)
    _execute_ddl(conn, SQL_CREATE_RECORD_MASTER_MAP_V3)
    _execute_ddl(conn, SQL_CREATE_RESOLUTION_QUEUE)

    # 3. Migrate existing master trials -> UUID
    cur = conn.execute("SELECT * FROM master_trials_legacy")
    uuid_map: dict[int, str] = {}
    for row in cur.fetchall():
        new_id = str(_uuid.uuid4())
        uuid_map[row["master_trial_id"]] = new_id
        conn.execute(
            """INSERT INTO master_trials
               (master_trial_id, preferred_title, scientific_title,
                study_type_id, status_id, conditions, interventions,
                enrollment, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id, row["preferred_title"], row["scientific_title"],
             row["study_type_id"], row["status_id"],
             row["conditions"], row["interventions"],
             row["enrollment"], row["created_at"], row["updated_at"]),
        )

    # 4. Migrate record_master_map
    if _table_exists(conn, "record_master_map_legacy"):
        cur = conn.execute("SELECT * FROM record_master_map_legacy")
        for row in cur.fetchall():
            new_uuid = uuid_map.get(row["master_trial_id"])
            if new_uuid is None:
                continue
            conn.execute(
                """INSERT INTO record_master_map
                   (record_id, master_trial_id, match_method, match_confidence,
                    match_status, created_at)
                   VALUES (?, ?, ?, ?, 'AUTO_CONFIRMED', ?)""",
                (row["record_id"], new_uuid,
                 row["match_method"], row["match_confidence"],
                 row["created_at"]),
            )

    # 5. Clear legacy INTEGER refs in change tracking tables
    for tbl in ("change_events", "trial_events"):
        if _column_exists(conn, tbl, "master_trial_id"):
            conn.execute(f"UPDATE {tbl} SET master_trial_id = NULL")


    conn.execute(
        "INSERT INTO schema_version (version, description) VALUES (?, ?)",
        (3, "Phase 3: UUID master trials, trial_identifiers, resolution_queue, match_status"),
    )
    logger.info("v3 migration complete (upgraded %d master trials, %d map entries).",
                len(uuid_map), conn.total_changes)


# ── v10 migration: change severity classification ─────────────────────────


def _migrate_v10(conn) -> None:
    """v10 migration — severity / change_type / importance_score on
    trial_events, backfilled through the ONE central classifier
    (core.severity.classify_event_row) so historic rows read identically to
    newly detected ones.  Idempotent: safe on fresh and upgraded databases.
    """
    from core.severity import classify_event_row

    logger.info("Running v10 migration (trial_events severity) …")
    for col in ("severity", "change_type", "importance_score"):
        if not _column_exists(conn, "trial_events", col):
            conn.execute(f"ALTER TABLE trial_events ADD COLUMN {col} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trial_events_severity "
        "ON trial_events(severity)"
    )

    status_labels = {
        str(r["status_type_id"]): r["label"]
        for r in conn.execute("SELECT status_type_id, label FROM status_types").fetchall()
    }
    # classify + update only rows that predate the columns (severity IS NULL)
    rows = conn.execute(
        "SELECT event_id, field_name, old_value, new_value, change_category "
        "FROM trial_events WHERE severity IS NULL"
    ).fetchall()
    for row in rows:
        result = classify_event_row(dict(row), status_labels)
        conn.execute(
            "UPDATE trial_events SET severity = ?, change_type = ?, importance_score = ? "
            "WHERE event_id = ?",
            (result.severity, result.change_type,
             f"{result.importance_score:g}", row["event_id"]),
        )
    logger.info("v10 migration complete (classified %d historic events).", len(rows))


# ── v5 migration: data quality hardening ────────────────────────────────


def _migrate_v5(conn) -> None:
    """v5 migration — backfill NULL FK refs, source_type, new columns.

    v5 fixes discovered during Phase 7 cross-source quality hardening:

    1. Ensure source_type is set on every registry_sources row.
    2. Backfill ``status_id`` for records where the raw status value didn't
       match the title-case lookup (e.g. "RECRUITING" → "Recruiting").
    3. Backfill ``study_type_id`` for records with the same case issue.
    4. Normalise ``study_phase`` values (e.g. "PHASE2" → "Phase 2").
    5. Add ``reviewed_by`` / ``reviewed_at`` / ``evidence_detail`` columns
       to ``resolution_queue`` if absent.
    6. Ensure ``trial_provenance`` / ``conflict_log`` exist (idempotent).
    """
    logger.info("Running v5 migration (data quality hardening) …")

    # Bail early if already applied
    cur = conn.execute(
        "SELECT version FROM schema_version WHERE version = 5 LIMIT 1"
    )
    if cur.fetchone():
        logger.info("v5 already applied, skipping.")
        return


    # ── 1. source_type for all sources ────────────────────────────────
    if not _column_exists(conn, "registry_sources", "source_type"):
        conn.execute(
            "ALTER TABLE registry_sources ADD COLUMN "
            "source_type TEXT NOT NULL DEFAULT 'PRIMARY'"
        )
    conn.execute(
        "UPDATE registry_sources SET source_type = 'AGGREGATOR' "
        "WHERE short_name = 'ICTRP'"
    )
    conn.execute(
        "UPDATE registry_sources SET source_type = 'PRIMARY' "
        "WHERE source_type IS NULL"
    )
    logger.info("  source_type ensured on all rows.")

    # ── 2. Backfill status_id ─────────────────────────────────────────
    # NCT API returns UPPERCASE raw values; DB status_types stores title case.
    # Since registry_records has no raw `status` column, we extract from raw_payload.
    # NOTE: raw_payload is pretty-printed JSON so patterns include `": "` (space after colon).
    _status_patterns: list[tuple[str, str]] = [
        ('"overallStatus": "RECRUITING"', "Recruiting"),
        ('"overallStatus": "ACTIVE, NOT RECRUITING"', "Active, not recruiting"),
        ('"overallStatus": "ACTIVE_NOT_RECRUITING"', "Active, not recruiting"),
        ('"overallStatus": "COMPLETED"', "Completed"),
        ('"overallStatus": "NOT_YET_RECRUITING"', "Not yet recruiting"),
        ('"overallStatus": "ENROLLING_BY_INVITATION"', "Enrolling by invitation"),
        ('"overallStatus": "SUSPENDED"', "Suspended"),
        ('"overallStatus": "TERMINATED"', "Terminated"),
        ('"overallStatus": "WITHDRAWN"', "Withdrawn"),
        ('"overallStatus": "UNKNOWN"', "Unknown status"),
        ('"overallStatus": "AVAILABLE"', "Available"),
        ('"overallStatus": "NO_LONGER_AVAILABLE"', "No longer available"),
        ('"overallStatus": "NO LONGER AVAILABLE"', "No longer available"),
        ('"overallStatus": "TEMPORARILY_NOT_AVAILABLE"', "Temporarily not available"),
        ('"overallStatus": "APPROVED_FOR_MARKETING"', "Approved for marketing"),
        ('"overallStatus": "WITHHELD"', "Unknown status"),
    ]
    cur = conn.execute(
        "SELECT count(*) FROM registry_records WHERE status_id IS NULL"
    )
    null_count = cur.fetchone()[0]
    if null_count:
        logger.info("  Backfilling %d NULL status_id rows …", null_count)
        for pattern, label in _status_patterns:
            conn.execute(
                """UPDATE registry_records
                   SET status_id = (SELECT status_type_id FROM status_types WHERE label = ?)
                   WHERE status_id IS NULL AND raw_payload LIKE ?""",
                (label, f"%{pattern}%"),
            )
        null_remaining = conn.execute(
            "SELECT count(*) FROM registry_records WHERE status_id IS NULL"
        ).fetchone()[0]
        logger.info("  NULL status_id remaining after backfill: %d", null_remaining)
    else:
        logger.info("  No NULL status_id rows found.")

    # ── 3. Backfill study_type_id ─────────────────────────────────────
    _stype_patterns: list[tuple[str, str]] = [
        ('"studyType": "INTERVENTIONAL"', "Interventional"),
        ('"studyType": "OBSERVATIONAL"', "Observational"),
        ('"studyType": "BASIC_SCIENCE"', "Basic Science"),
        ('"studyType": "DIAGNOSTIC_TEST"', "Diagnostic Test"),
        ('"studyType": "HEALTH_SERVICES_RESEARCH"', "Health Services Research"),
        ('"studyType": "PREVENTION"', "Prevention"),
        ('"studyType": "SCREENING"', "Screening"),
        ('"studyType": "OTHER"', "Other"),
        ('"studyType": "EXPANDED_ACCESS"', "Other"),
        ('"studyType": "NA"', "Other"),
    ]
    cur = conn.execute(
        "SELECT count(*) FROM registry_records WHERE study_type_id IS NULL"
    )
    null_count = cur.fetchone()[0]
    if null_count:
        logger.info("  Backfilling %d NULL study_type_id rows …", null_count)
        for pattern, label in _stype_patterns:
            conn.execute(
                """UPDATE registry_records
                   SET study_type_id = (SELECT study_type_id FROM study_types WHERE label = ?)
                   WHERE study_type_id IS NULL AND raw_payload LIKE ?""",
                (label, f"%{pattern}%"),
            )
        null_remaining = conn.execute(
            "SELECT count(*) FROM registry_records WHERE study_type_id IS NULL"
        ).fetchone()[0]
        logger.info("  NULL study_type_id remaining after backfill: %d", null_remaining)
    else:
        logger.info("  No NULL study_type_id rows found.")

    # ── 4. Normalise study_phase (raw → human-readable) ───────────────
    _phase_fixes = {
        "PHASE1": "Phase 1",
        "PHASE2": "Phase 2",
        "PHASE3": "Phase 3",
        "PHASE4": "Phase 4",
        "PHASE1,PHASE2": "Phase 1/Phase 2",
        "PHASE2,PHASE3": "Phase 2/Phase 3",
        "PHASE1,PHASE2,PHASE3": "Phase 1/Phase 2/Phase 3",
        "EARLY_PHASE1": "Early Phase 1",
    }
    for old_val, new_val in _phase_fixes.items():
        conn.execute(
            "UPDATE registry_records SET study_phase = ? WHERE study_phase = ?",
            (new_val, old_val),
        )
    logger.info("  study_phase normalised.")

    # ── 5. Add columns to resolution_queue ────────────────────────────
    for col in ("reviewed_by", "reviewed_at", "evidence_detail"):
        if not _column_exists(conn, "resolution_queue", col):
            col_type = "TEXT"
            conn.execute(
                f"ALTER TABLE resolution_queue ADD COLUMN {col} {col_type}"
            )
            logger.info("  Added resolution_queue.%s", col)

    # ── 6. Ensure provenance / conflict tables exist ──────────────────
    for ddl in (SQL_CREATE_TRIAL_PROVENANCE, SQL_CREATE_CONFLICT_LOG):
        _execute_ddl(conn, ddl)


    conn.execute(
        "INSERT INTO schema_version (version, description) VALUES (?, ?)",
        (5, "Phase 7: data quality hardening — backfill NULL refs, "
            "source_type, study_phase normalisation, review_queue columns"),
    )
    logger.info("v5 migration complete.")


# ── Seed data ────────────────────────────────────────────────────────────

def _seed_lookups(conn) -> None:
    known_sources = [
        ("NCT",   "ClinicalTrials.gov"),
        ("CTR",   "中国药物临床试验登记与信息公示平台"),
        ("ChiCTR","中国临床试验注册中心"),
        ("ICTRP", "WHO ICTRP"),
        ("CTIS",  "EU Clinical Trials Information System (CTIS)"),
        ("ISRCTN", "ISRCTN Registry"),
        ("EUCTR", "EU Clinical Trials Register (historical)"),
    ]
    for short, full in known_sources:
        source_type = "AGGREGATOR" if short == "ICTRP" else "PRIMARY"
        conn.execute(
            "INSERT OR IGNORE INTO registry_sources (short_name, full_name, source_type) VALUES (?, ?, ?)",
            (short, full, source_type),
        )
        # Ensure existing ICTRP row gets updated if it was created before v4
        if short == "ICTRP":
            conn.execute(
                "UPDATE registry_sources SET source_type = 'AGGREGATOR' WHERE short_name = 'ICTRP' AND source_type IS NULL"
            )

    known_study_types = [
        "Interventional", "Observational", "Observational [Patient Registry]",
        "Diagnostic Test", "Prevention", "Screening",
        "Basic Science", "Health Services Research", "Other",
    ]
    for label in known_study_types:
        conn.execute("INSERT OR IGNORE INTO study_types (label) VALUES (?)", (label,))

    known_statuses = [
        "Not yet recruiting", "Recruiting", "Enrolling by invitation",
        "Active, not recruiting", "Completed", "Suspended", "Terminated",
        "Withdrawn", "Unknown status", "Approved for marketing",
        "Temporarily not available", "No longer available",
        "Pre-registration", "Registered",
    ]
    for label in known_statuses:
        conn.execute("INSERT OR IGNORE INTO status_types (label) VALUES (?)", (label,))


def get_table_info() -> list[dict]:
    """Return list of {name, row_count} for every user table."""
    conn = get_connection()
    tables = []
    for name, _ in ALL_TABLES:
        try:
            cur = conn.execute(f"SELECT count(*) FROM {name}")
            count = cur.fetchone()[0]
        except Exception:
            count = -1  # table might not exist yet
        tables.append({"name": name, "rows": count})
    return tables
