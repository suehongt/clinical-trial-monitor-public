"""FastAPI read-only API over the local registry database (Phase 7 P1).

Architecture (the project design notes §3/§5):

    browser(React SPA) → this API → SQLite db/ct_monitor.db (pre-crawled)
                                          ↑
                       scheduled pipeline (launchd/cron) ─┘

Every endpoint is a pure read of the local library — a user click never
triggers a registry crawl (ChiCTR/CTR WAF red line).  Responses reuse the
exporter's JSON contract (Trial / TrialDetailData / MirrorGroup /
ChangeEvent) so ``web/src/guards.ts`` validates both static JSON files and
API payloads unchanged, plus ``generated_at`` and ``data_as_of`` (per-source
last successful sync from ``sync_status``).

Query semantics are the report package's own: ``ct_report.query.query_trials``
(FTS prefilter + word-boundary re-filter + AMI disambiguation) for search,
``scripts.export_report_json`` shaping for list entries / mirrors / events,
``ct_report.query.get_events_for_record`` for per-trial change history.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Robust against `uvicorn` launched from outside the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from core.version import APP_VERSION
from fastapi.staticfiles import StaticFiles
from requests import get as http_get

from config import CONFIG, DISEASE_PROFILES
from core.digest import collect_new_trials
from core.refresh import refresh_queue_stats
from core.waf_guard import WafCircuitOpenError
from ct_report.query import (
    _TRIALS_SELECT,
    fetch_cross_source_siblings,
    query_trials,
)
from scripts.export_report_json import (
    _detail_extractors,
    build_detail,
    build_endpoint_table,
    build_events,
    build_light_trial,
    build_mirrors,
    build_profile_payload,
)

logger = logging.getLogger("server")

DEFAULT_PAGE_SIZE = 0  # 0 = no pagination (static-JSON-compatible full list)

# This deliberately maps the canonical Phase-1 field names rather than
# creating a parallel watch taxonomy.  Preferences only filter/highlight the
# existing event stream; ingestion, diffing and severity remain untouched.
WATCH_FIELD_GROUPS = {
    "status_id": "status", "enrollment": "enrollment",
    "start_date": "dates", "primary_completion_date": "dates",
    "completion_date": "dates", "registration_date": "dates",
    "primary_endpoint": "primary_outcomes", "secondary_endpoints": "secondary_outcomes",
    "interventions": "interventions", "arm_group_interventions": "arms",
    "eligibility_criteria": "eligibility", "sponsors": "sponsor",
    "countries": "countries", "locations": "sites", "contacts": "contacts",
    "study_design": "study_design", "study_phase": "study_design",
}
WATCH_DEFAULTS = {
    "status", "enrollment", "dates", "primary_outcomes", "interventions",
    "eligibility", "sponsor", "countries", "sites", "study_design",
}
WATCH_ALL_GROUPS = (
    "status", "enrollment", "dates", "primary_outcomes", "secondary_outcomes",
    "interventions", "arms", "eligibility", "sponsor", "collaborators",
    "countries", "sites", "contacts", "study_design", "other",
)
SEVERITY_RANK = {"critical": 4, "important": 3, "normal": 2, "minor": 1}

# Human-readable field labels for change presentation (Phase 3E).  The
# frontend keeps an identical fallback map for static exports; the server
# version is authoritative in API mode because the field names are the
# canonical Phase-1 diff columns (WATCH_FIELD_GROUPS keys plus close kin).
FIELD_LABELS: Dict[str, tuple] = {
    # field_name: (english label, chinese label)
    "status_id": ("Recruitment Status", "招募状态"),
    "enrollment": ("Enrollment", "入组人数"),
    "start_date": ("Start Date", "开始日期"),
    "primary_completion_date": ("Primary Completion Date", "主要完成日期"),
    "completion_date": ("Completion Date", "研究完成日期"),
    "registration_date": ("Registration Date", "注册日期"),
    "primary_endpoint": ("Primary Outcome", "主要终点指标"),
    "secondary_endpoints": ("Secondary Outcomes", "次要终点指标"),
    "interventions": ("Interventions", "干预措施"),
    "arm_group_interventions": ("Arm Interventions", "分组干预"),
    "eligibility_criteria": ("Eligibility Criteria", "入选排除标准"),
    "sponsors": ("Sponsor", "申办方"),
    "collaborators": ("Collaborators", "合作方"),
    "countries": ("Countries", "国家/地区"),
    "locations": ("Locations", "研究地点"),
    "contacts": ("Contacts", "联系方式"),
    "study_design": ("Study Design", "研究设计"),
    "study_phase": ("Study Phase", "研究分期"),
    "conditions": ("Conditions", "适应症"),
    "title": ("Title", "研究题目"),
    "scientific_title": ("Scientific Title", "科学题目"),
    "last_updated_at_source": ("Last Updated at Source", "注册库更新时间"),
}


def _field_label(field_name: str) -> tuple:
    return FIELD_LABELS.get(field_name, (field_name, field_name))

# NCT live proxy (Phase 7 P2): public-API politeness — cached answers and a
# floor between outbound requests; page sizes stay small (thin proxy only).
NCT_LIVE_TTL_SEC = 600
NCT_LIVE_MIN_INTERVAL_SEC = 2.0

# ChiCTR has no public API.  These limits protect the local HTML-adapter
# endpoints from turning an accidental client loop into registry load.
CHICTR_API_TTL_SEC = 300
CHICTR_API_MIN_INTERVAL_SEC = 2.0

# China Drug Trials/CDE likewise has no documented public JSON API and is
# more WAF-sensitive than ChiCTR.  Keep its unofficial HTML adapter isolated
# behind its own cache, lock, and conservative outbound interval.
CTR_API_TTL_SEC = 300
CTR_API_MIN_INTERVAL_SEC = 5.0

# WHO's public Search Portal is an ASP.NET form rather than an open API.  The
# adapter below is intentionally low-frequency: WHO itself refreshes ICTRP
# weekly, so repeated queries within an hour provide no useful freshness.
ICTRP_API_TTL_SEC = 3600
ICTRP_API_MIN_INTERVAL_SEC = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _open_conn() -> sqlite3.Connection:
    """Fresh per-request connection.

    Deliberately NOT the thread-local cache in db.connection: uvicorn and
    TestClient both run sync handlers on a thread pool, and a cached
    connection would pin a stale database path across tests / overrides.
    The path is read at call time from ct_report.query (the same source the
    report queries use), so one override point covers every layer.
    """
    import ct_report.query as q

    conn = sqlite3.connect(str(q.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply_db_override(db_path: str) -> None:
    """Point every query layer (report + connection + server) at ``db_path``."""
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"database path does not exist: {path}")
    import ct_report.query as q
    from ct_report import paths as report_paths
    from config import CONFIG

    report_paths.DB_PATH = path
    q.DB_PATH = path
    CONFIG.db.path = path
    logger.info("database override applied: %s", path)


def _data_as_of(conn: sqlite3.Connection) -> Dict[str, Optional[str]]:
    """Per-source data freshness — the honest "data as of" statement.

    sync_status.last_successful_sync is authoritative where it exists
    (NCT); sources that never write it (ChiCTR/CTR/ICTRP discovery-enrich
    path) fall back to their newest crawled record so the stamp never reads
    as "never" while the library demonstrably holds fresh data.
    """
    rows = conn.execute(
        """SELECT s.short_name,
                  ss.last_successful_sync AS synced,
                  (SELECT MAX(r.last_crawled_at) FROM registry_records r
                   WHERE r.source_id = s.source_id AND r.is_latest = 1) AS crawled
           FROM registry_sources s
           LEFT JOIN sync_status ss ON ss.source_id = s.source_id
           ORDER BY s.short_name"""
    ).fetchall()

    def stamp(v: Any) -> str:
        # normalise ISO "T" separators so lexicographic max is meaningful
        return v.replace("T", " ") if isinstance(v, str) else ""

    out: Dict[str, Optional[str]] = {}
    for r in rows:
        candidates = [t for t in (stamp(r["synced"]), stamp(r["crawled"])) if t]
        out[r["short_name"]] = max(candidates) if candidates else None
    return out


class _DataCache:
    """Process-local memo invalidated by SQLite's data_version counter.

    The pipeline writes from another process; every cache read first asks a
    long-lived watcher connection for ``PRAGMA data_version``, which bumps
    when any other connection has committed — no TTL guessing, no stale
    payloads after a pipeline run.  Identical concurrent requests are
    single-flighted per key: the first caller builds outside the shared lock
    while duplicates wait, so a slow cold build (tens of seconds for the
    intelligence aggregates on the small demo box) never freezes unrelated
    endpoints behind the lock.
    """

    def __init__(self, db_path: str, max_entries: int = 256) -> None:
        # created in create_app's thread, read from uvicorn/TestClient worker
        # threads — every access is serialized under the lock below
        self._watch = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._version: Optional[int] = None
        self._store: Dict[Any, Any] = {}
        self._lock = threading.Lock()
        # The search/dashboard/intelligence endpoints cache one entry per
        # distinct query; the cap keeps long-tail browsing from growing the
        # store without bound.
        self._max_entries = max_entries
        self._inflight: Dict[Any, threading.Event] = {}

    def get(self, key: Any, builder: Callable[[], Any]) -> Any:
        for _ in range(2):
            with self._lock:
                version = self._watch.execute("PRAGMA data_version").fetchone()[0]
                if version != self._version:
                    self._store.clear()
                    self._version = version
                if key in self._store:
                    return self._store[key]
                done = self._inflight.get(key)
                owner = done is None
                if owner:
                    done = threading.Event()
                    self._inflight[key] = done
            if not owner:
                done.wait()
                continue
            try:
                value = builder()
            except BaseException:
                with self._lock:
                    self._inflight.pop(key, None)
                done.set()
                raise
            with self._lock:
                self._store[key] = value
                while len(self._store) > self._max_entries:
                    self._store.pop(next(iter(self._store)))
                self._inflight.pop(key, None)
            done.set()
            return value
        # Only reachable if every observed owner kept raising; the final
        # attempt's exception is the honest one, so this is a guard rail.
        raise RuntimeError(f"data cache builder for {key!r} did not produce a value")

    def close(self) -> None:
        with self._lock:
            self._watch.close()
            self._store.clear()


def _shaped_trials(profile: str) -> List[Dict[str, Any]]:
    """Full shaped (light) trial list for a profile — the cacheable unit."""
    rows = query_trials(keywords=DISEASE_PROFILES[profile]["report_keywords"])
    return [build_light_trial(t) for t in rows]


def _nct_live_trial(raw: Dict[str, Any]) -> Dict[str, Any]:
    """One ClinicalTrials.gov API v2 study JSON -> viewer Trial shape.

    Reuses the collector's normalise() so live results parse identically to
    crawled ones (status/phase normalisation, date formats, sponsor list) —
    there is deliberately no second parser to drift.
    """
    from collectors.clinicaltrials import ClinicalTrialsGovCollector

    rec = ClinicalTrialsGovCollector().normalise(raw)
    row = {
        "source_trial_id": rec.source_trial_id,
        "title": rec.title,
        "scientific_title": rec.scientific_title,
        "enrollment": rec.enrollment,
        "registration_date": rec.registration_date,
        "start_date": rec.start_date,
        "completion_date": rec.completion_date,
        "last_updated_at_source": rec.last_updated_at_source,
        "conditions": rec.conditions,
        "interventions": rec.interventions,
        "sponsors": rec.sponsors,
        "locations": rec.locations,
        "countries": rec.countries,
        "study_phase": rec.study_phase,
        "status_label": rec.status,
        "study_type_label": rec.study_type,
        "source_url": rec.source_url,
        "primary_endpoint": rec.primary_endpoint,
        "secondary_endpoints": rec.secondary_endpoints,
        "short_name": "NCT",
        "raw_payload": None,
    }
    return build_light_trial(row)


def _build_daily(
    events: List[Dict[str, Any]],
    new_trials: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Day-grouped activity view: 日期 → 研究归组的字段级变更 + 当日新增。

    The shape the timeline renders directly:
      [{date, change_count, new_count,
        trials: [{id, source, title, url, changes: [field diffs…]}],
        new_trials: [{id, source, title, url, first_crawled_at, …}]}]
    Newest day first; within a day, trials with the most changes lead.
    """
    days: Dict[str, Dict[str, Any]] = {}

    def day(date_str: Optional[str]) -> Dict[str, Any]:
        key = (date_str or "")[:10] or "unknown"
        if key not in days:
            days[key] = {
                "date": key,
                "change_count": 0,
                "new_count": 0,
                "trials": {},
                "new_trials": [],
            }
        return days[key]

    for e in events:
        d = day(e.get("detected_at"))
        key = (e.get("source") or "", e.get("id") or "")
        t = d["trials"].get(key)
        if t is None:
            t = {
                "id": e.get("id") or "",
                "source": e.get("source") or "",
                "title": e.get("title") or "",
                "url": e.get("source_url"),
                "changes": [],
            }
            d["trials"][key] = t
        t["changes"].append({
            "field_name": e.get("field_name") or "",
            "old_value": e.get("old_value"),
            "new_value": e.get("new_value"),
            "change_category": e.get("change_category"),
            "detected_at": e.get("detected_at") or "",
            "field_label": _field_label(e.get("field_name") or "")[0],
            "field_label_zh": _field_label(e.get("field_name") or "")[1],
        })
        d["change_count"] += 1

    for n in new_trials:
        d = day(n.get("first_crawled_at"))
        d["new_trials"].append({
            "id": n.get("source_trial_id") or "",
            "source": n.get("short_name") or "",
            "title": n.get("title") or "",
            "url": n.get("source_url"),
            "enrollment": n.get("enrollment"),
            "phase": n.get("study_phase"),
            "first_crawled_at": n.get("first_crawled_at") or "",
        })
        d["new_count"] += 1

    out: List[Dict[str, Any]] = []
    for key in sorted(days, reverse=True):
        d = days[key]
        trials = sorted(d["trials"].values(),
                        key=lambda t: (-len(t["changes"]), t["id"]))
        out.append({
            "date": d["date"],
            "change_count": d["change_count"],
            "new_count": d["new_count"],
            "trials": trials,
            "new_trials": d["new_trials"],
        })
    return out


def _status_key(status: str) -> str:
    """Collapse status labels into filter buckets — same rules as
    web/src/labels.ts statusKey(), so ?status= matches the viewer chips."""
    if re.search(
        r"Not yet recruiting|Active,\s*not recruiting|尚未招募|未开始招募|"
        r"不再招募|招募(?:结束|完成|停止|暂停)",
        status,
        re.I,
    ):
        return "other"
    if re.search(r"Recruiting|招募|正在进行", status, re.I):
        return "recruiting"
    if re.search(r"Completed|完成", status, re.I):
        return "completed"
    if re.search(r"Terminat|终止|停止", status, re.I):
        return "terminated"
    return "other"


def _require_profile(profile: str) -> str:
    if profile not in DISEASE_PROFILES:
        known = ", ".join(sorted(DISEASE_PROFILES))
        raise HTTPException(status_code=404,
                            detail=f"unknown profile '{profile}' (known: {known})")
    return profile


def _record_change_history(
    conn: sqlite3.Connection,
    source: str,
    trial_id: str,
    latest_row: sqlite3.Row,
) -> List[Dict[str, Any]]:
    """Full tracked timeline of one trial, oldest first.

    Spans the trial's WHOLE version chain AND all registries sharing the
    trial ID (ICTRP mirrors reuse the source registry's ID): events are
    detected on whichever record changed, and the timeline of "this trial"
    must not go dark just because the user opened a mirror card.  status_id
    FK values are translated to labels (shared helper, same as the global
    stream).
    """
    from ct_report.query import translate_status_event_values

    base = {
        "id": trial_id or "",
        "source": source or "",
        "title": latest_row["title"] or "",
        "source_url": latest_row["source_url"],
    }
    rows = [dict(r) for r in conn.execute(
        """SELECT te.field_name, te.old_value, te.new_value,
                  te.change_category, te.detected_at
           FROM trial_events te
           JOIN registry_records r ON r.record_id = te.record_id
           WHERE r.source_trial_id = ?
           ORDER BY te.detected_at ASC, te.event_id ASC""",
        (trial_id,),
    ).fetchall()]
    rows = translate_status_event_values(rows, conn)
    history: List[Dict[str, Any]] = []
    for ev in rows:
        history.append({
            "detected_at": ev.get("detected_at") or "",
            "field_name": ev.get("field_name") or "",
            "old_value": ev.get("old_value"),
            "new_value": ev.get("new_value"),
            "change_category": ev.get("change_category"),
            **base,
        })
    return history


def _field_group(field_name: str) -> str:
    return WATCH_FIELD_GROUPS.get(field_name, "other")


def _watch_preferences(conn: sqlite3.Connection, watch_id: int) -> List[Dict[str, Any]]:
    return [dict(r) | {"enabled": bool(r["enabled"])} for r in conn.execute(
        "SELECT field_group, enabled, minimum_severity FROM watch_preferences "
        "WHERE trial_watch_id=? ORDER BY id", (watch_id,)
    ).fetchall()]


def _watch_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    out["preferences"] = _watch_preferences(conn, out["id"])
    return out


def _require_trial(conn: sqlite3.Connection, source: str, trial_id: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT r.record_id, r.source_trial_id FROM registry_records r
           JOIN registry_sources s ON s.source_id=r.source_id
           WHERE r.is_latest=1 AND s.short_name=? AND r.source_trial_id=?""",
        (source, trial_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no latest record for {source}:{trial_id}")
    return row


def _event_rows(conn: sqlite3.Connection, trial_id: str, *, limit: int = 100,
                offset: int = 0, severity: Optional[str] = None,
                field_group: Optional[str] = None) -> tuple[List[Dict[str, Any]], int]:
    """One query path for trial Changes and Timeline; raw payload is never selected."""
    from ct_report.query import translate_status_event_values

    where = ["r.source_trial_id=?"]
    args: List[Any] = [trial_id]
    if severity:
        where.append("COALESCE(te.severity, 'normal')=?")
        args.append(severity)
    # Group is derived from the canonical field name; filter in Python only
    # after a bounded query so there is no duplicate watch-specific diff logic.
    sql_where = " AND ".join(where)
    rows = [dict(r) for r in conn.execute(
        f"""SELECT te.event_id, te.record_id, te.field_name, te.old_value, te.new_value,
                    te.change_category, te.change_type, COALESCE(te.severity, 'normal') severity,
                    te.importance_score, te.detected_at, r.version_number,
                    r.last_updated_at_source source_updated_at, s.short_name source, r.source_trial_id id
             FROM trial_events te JOIN registry_records r ON r.record_id=te.record_id
             JOIN registry_sources s ON s.source_id=r.source_id
             WHERE {sql_where} ORDER BY te.detected_at DESC, te.event_id DESC""", args,
    ).fetchall()]
    rows = translate_status_event_values(rows, conn)
    for row in rows:
        row["field_group"] = _field_group(row["field_name"])
        row["field_label"], row["field_label_zh"] = _field_label(row["field_name"])
        row["from_version"] = max(1, (row["version_number"] or 1) - 1)
        row["to_version"] = row["version_number"]
    if field_group:
        rows = [r for r in rows if r["field_group"] == field_group]
    total = len(rows)
    return rows[offset:offset + limit], total


def _translate_status_value(conn: sqlite3.Connection, value: Any) -> Any:
    """status_id FK → label for presentation (raw values stay untouched elsewhere)."""
    if value is None or not isinstance(value, int):
        return value
    row = conn.execute(
        "SELECT label FROM status_types WHERE status_type_id = ?", (value,)).fetchone()
    return row["label"] if row is not None else value


def _compare_records(old: sqlite3.Row, new: sqlite3.Row,
                     conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Comparison presentation reuses Phase-1 compare_values, never a new diff engine."""
    from core.change_detection import FIELD_CATEGORY, compare_values

    tracked = tuple(WATCH_FIELD_GROUPS) + ("conditions", "last_updated_at_source")
    # WATCH_FIELD_GROUPS is a superset of the record columns (e.g. "contacts"
    # has no dedicated column); compare only what both rows actually carry.
    available = set(old.keys()) & set(new.keys())
    out: List[Dict[str, Any]] = []
    for field in tracked:
        if field not in available:
            continue
        old_value, new_value = old[field], new[field]
        if not compare_values(old_value, new_value, field):
            continue
        if conn is not None and field == "status_id":
            old_value = _translate_status_value(conn, old_value)
            new_value = _translate_status_value(conn, new_value)
        item: Dict[str, Any] = {
            "field_name": field, "field_group": _field_group(field),
            "old_value": old_value, "new_value": new_value,
            "change_category": FIELD_CATEGORY.get(field, "other"),
            "change_type": "added" if old_value in (None, "") else ("removed" if new_value in (None, "") else "modified"),
        }
        if field == "enrollment":
            try:
                before, after = float(old_value), float(new_value)
                item["absolute_change"] = after - before
                if before:
                    item["percent_change"] = round((after - before) / before * 100, 1)
            except (TypeError, ValueError):
                pass
        if field in {"start_date", "primary_completion_date", "completion_date", "registration_date"}:
            try:
                item["date_shift_days"] = (datetime.fromisoformat(str(new_value)[:10]) - datetime.fromisoformat(str(old_value)[:10])).days
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """Build the app (optionally overriding the database location).

    Tests pass a temp database via the ``test_db`` fixture (plus the
    ``ct_report.query.DB_PATH`` monkeypatch, same as the exporter tests);
    ``python -m server`` passes the CT_DB_PATH environment variable.
    """
    if db_path:
        _apply_db_override(db_path)

    import ct_report.query as q
    from core.integrity import open_readonly, startup_check
    mode = os.environ.get("CT_MODE", "development").lower()
    if mode not in {"development", "production", "test"}:
        raise RuntimeError("CT_MODE must be development, production or test")
    with closing(open_readonly(q.DB_PATH)) as startup_conn:
        startup_check(startup_conn)
    if mode == "production":
        from core.email_provider import validate_email_configuration
        validate_email_configuration()
        if not os.access(q.DB_PATH, os.W_OK) or not os.access(Path(q.DB_PATH).parent, os.W_OK):
            raise RuntimeError("database and parent directory must be writable in production")
    cache = _DataCache(str(q.DB_PATH))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            cache.close()

    app = FastAPI(
        title="Clinical Trial Monitor API",
        description="Read-only queries over the locally crawled registry database.",
        version=APP_VERSION,
        docs_url=None if mode == "production" else "/api/docs",
        openapi_url=None if mode == "production" else "/api/openapi.json",
        lifespan=lifespan,
    )
    # Trial lists and detail maps are megabytes of JSON — compress in transit.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.exception_handler(Exception)
    async def production_error(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error("request failed request_id=%s operation=%s error_type=%s",
                     request_id, request.url.path, type(exc).__name__)
        if mode != "production":
            raise exc
        return JSONResponse(status_code=500, headers={"X-Request-ID": request_id,
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}, content={"error": {
            "code": "INTERNAL_ERROR", "message": "Internal server error.",
            "request_id": request_id}})

    @app.exception_handler(HTTPException)
    async def api_http_error(request: Request, exc: HTTPException):
        if mode != "production":
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST" if exc.status_code < 500 else "SERVICE_UNAVAILABLE"
        message = "Not found." if code == "NOT_FOUND" else "Invalid request." if code == "INVALID_REQUEST" else "Service unavailable."
        return JSONResponse(status_code=exc.status_code, content={"error": {
            "code": code, "message": message, "request_id": getattr(request.state, "request_id", "unknown")}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if mode != "production":
            from fastapi.encoders import jsonable_encoder
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
        return JSONResponse(status_code=422, content={"error": {
            "code": "INVALID_REQUEST", "message": "Invalid request.",
            "request_id": getattr(request.state, "request_id", "unknown")}})

    @app.middleware("http")
    async def no_store_api(request, call_next):
        """API data changes after every pipeline run — never cache it."""
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        if mode == "production" and request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={"error": {"code": "ORIGIN_FORBIDDEN",
                    "message": "Cross-origin writes are not allowed.", "request_id": request_id}},
                    headers={"X-Request-ID": request_id})
        if request.method in {"POST", "PATCH", "PUT"}:
            size = request.headers.get("content-length")
            if size and (not size.isdigit() or int(size) > 65536):
                return JSONResponse(status_code=413, content={"error": {"code": "REQUEST_TOO_LARGE",
                    "message": "Request body exceeds 64 KiB.", "request_id": request_id}},
                    headers={"X-Request-ID": request_id})
            # Content-Length is optional (for example with chunked transfer).
            # Bound the actual stream before FastAPI parses or buffers JSON.
            chunks = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > 65536:
                    return JSONResponse(status_code=413, content={"error": {"code": "REQUEST_TOO_LARGE",
                        "message": "Request body exceeds 64 KiB.", "request_id": request_id}},
                        headers={"X-Request-ID": request_id})
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        logger.info("request_id=%s operation=%s method=%s status=%s duration_ms=%.1f",
                    request_id, request.url.path, request.method, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response

    @app.middleware("http")
    async def basic_auth_guard(request, call_next):
        """Shared-credential gate for LAN/server deployments.

        Off by default: with CT_AUTH_USER / CT_AUTH_PASSWORD unset the app
        behaves exactly as before (single-user localhost behind a trusted
        boundary).  When both are set, every request except the
        /api/health liveness probe must carry matching HTTP Basic
        credentials — the browser's native prompt means the SPA needs zero
        changes.  Declared after no_store_api so it runs outermost and
        rejects before any body buffering.  Env is read per request so
        credentials can be rotated via the systemd EnvironmentFile.
        """
        user = os.environ.get("CT_AUTH_USER", "")
        password = os.environ.get("CT_AUTH_PASSWORD", "")
        if user and password and request.url.path != "/api/health":
            request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
            supplied = request.headers.get("authorization", "")
            ok = False
            if supplied.startswith("Basic "):
                try:
                    decoded = base64.b64decode(supplied[6:].strip(), validate=True).decode("utf-8")
                    given_user, _, given_pass = decoded.partition(":")
                    # compare_digest needs bytes when credentials are non-ASCII
                    ok = (secrets.compare_digest(given_user.encode("utf-8"), user.encode("utf-8"))
                          and secrets.compare_digest(given_pass.encode("utf-8"), password.encode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="ct-monitor", charset="UTF-8"',
                             "X-Request-ID": request_id,
                             "X-Content-Type-Options": "nosniff"},
                    content={"error": {"code": "UNAUTHORIZED",
                                       "message": "Authentication required.",
                                       "request_id": request_id}})
        return await call_next(request)

    @app.middleware("http")
    async def public_readonly_guard(request, call_next):
        """Read-only gate for deployments exposed to the open internet.

        With CT_PUBLIC_READONLY=1 (intended for the public demo box, where
        Basic auth is switched off), every GET/HEAD/OPTIONS passes and every
        mutating method is rejected with 403 — except two whitelisted
        endpoints that support *browsing* without touching crawl or user
        state: POST /api/search/interpret (stateless local parser behind
        the "Ask naturally" search) and POST /api/saved-searches/{id}/open
        (updates last_opened_at only).  This keeps the crawl triggers
        (registry sync, run retry, live-check) and all data mutation
        (watches, monitors, projects, notes, notifications) unreachable
        from anonymous traffic.

        Live registry-adapter GETs are blocked too: anonymous visitors must
        not be able to turn the demo box into an outbound proxy or consume the
        source-specific WAF/rate-limit budget.  No SPA surface calls them on a
        read-only deployment.  Declared after basic_auth_guard so it runs
        outermost; env is read per request like the auth gate.
        """
        if os.environ.get("CT_PUBLIC_READONLY", "").strip().lower() not in ("1", "true", "yes"):
            return await call_next(request)
        path = request.url.path
        live_adapter_get = (
            path == "/api/nct/live"
            or path.startswith("/api/nct/study/")
            or path == "/api/chictr/search"
            or path.startswith("/api/chictr/studies/")
            or path == "/api/ctr/search"
            or path.startswith("/api/ctr/studies/")
            or path == "/api/ictrp/search"
        )
        if request.method in ("GET", "HEAD") and live_adapter_get:
            request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
            return JSONResponse(
                status_code=403,
                headers={"X-Request-ID": request_id,
                         "X-Content-Type-Options": "nosniff"},
                content={"error": {"code": "FORBIDDEN_READONLY",
                                   "message": "This is a read-only public demo; live source checks are disabled.",
                                   "request_id": request_id}})
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        if request.method == "POST" and (
            path == "/api/search/interpret"
            or re.fullmatch(r"/api/saved-searches/\d+/open", path)
        ):
            return await call_next(request)
        request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
        return JSONResponse(
            status_code=403,
            headers={"X-Request-ID": request_id,
                     "X-Content-Type-Options": "nosniff"},
            content={"error": {"code": "FORBIDDEN_READONLY",
                               "message": "This is a read-only public demo; changes are disabled.",
                               "request_id": request_id}})

    @app.get("/api/health")
    def health() -> dict:
        """Liveness + capability probe — the SPA's service-state check.

        ``status`` is "ok" only when a real query against the configured
        database succeeds, so the frontend never infers backend health from
        a bare 200.  A broken database yields HTTP 503 with
        ``status: "database_error"`` (distinguished client-side from
        "offline"); ``schema_version`` lets the SPA flag an incompatible
        backend instead of mis-parsing its payloads.
        """
        import ct_report.query as q

        try:
            with closing(_open_conn()) as conn:
                conn.execute("SELECT 1").fetchone()
                row = conn.execute(
                    "SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1"
                ).fetchone()
        except Exception as exc:
            logger.error("health check failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={"status": "database_error", "database": None,
                        "generated_at": _now()},
            ) from exc
        return {
            "status": "ok",
            "app_version": app.version,
            "generated_at": _now(),
            "database": None if mode == "production" else str(q.DB_PATH),
            "schema_version": int(row["version"]) if row is not None else None,
        }

    @app.get("/api/ready")
    def ready() -> dict:
        try:
            with closing(_open_conn()) as conn:
                state = startup_check(conn)
            return {"status": "ready", "app_version": app.version, **state}
        except (sqlite3.DatabaseError, RuntimeError) as exc:
            logger.error("readiness failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="database not ready") from exc

    @app.get("/api/profiles")
    def profiles() -> dict:
        """Disease catalogue with live totals — same shape as data/index.json."""
        out = []
        for key, meta in DISEASE_PROFILES.items():
            items = cache.get(("trials", key), lambda p=key: _shaped_trials(p))
            out.append({
                "key": key,
                "label": meta["label"],
                "label_en": meta["label_en"],
                "total": len(items),
                "file": f"{key}.json",
            })
        with closing(_open_conn()) as conn:
            data_as_of = _data_as_of(conn)
        return {"generated_at": _now(), "data_as_of": data_as_of, "diseases": out}

    @app.get("/api/profiles/{profile}/details")
    def profile_details(profile: str) -> dict:
        """Whole-profile detail map — the <key>_details.json contract.

        Keyed by "<source>:<trial_id>" (source-scoped: ICTRP mirrors reuse the
        source registry's ID, a bare id would collide).  Served via the
        exporter's build_profile_payload so static JSON and API can never
        drift apart.  The body is a BARE map — no generated_at/data_as_of
        wrapper keys (the viewer's isDetailsMap guard rejects non-detail
        values, exactly as it does for the static file).
        """
        _require_profile(profile)
        details = cache.get(
            ("details", profile), lambda: build_profile_payload(profile)[1])
        return details

    @app.post("/api/search/interpret")
    def interpret_search(payload: Dict[str, Any] = Body(...)) -> dict:
        """Propose, but never execute, a validated deterministic search rule."""
        from core.query_interpreter import interpret
        if set(payload) != {"text"}:
            raise HTTPException(status_code=422, detail="expected text only")
        try:
            return interpret(payload["text"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Search interpretation failed")
            raise HTTPException(status_code=503, detail="search interpretation unavailable") from exc

    def _project(conn: sqlite3.Connection, project_id: int) -> dict:
        from core.projects import project_row
        try:
            return project_row(conn, project_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/projects")
    def list_projects(include_archived: bool = False) -> dict:
        from core.projects import list_summaries
        with closing(_open_conn()) as conn:
            return {"generated_at": _now(), "projects": list_summaries(conn, include_archived)}

    @app.post("/api/projects", status_code=201)
    def create_project(payload: Dict[str, Any] = Body(...)) -> dict:
        from core.projects import detail
        name = payload.get("name")
        description = payload.get("description", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or not isinstance(description, str) or len(description) > 2000:
            raise HTTPException(status_code=422, detail="invalid project name or description")
        with closing(_open_conn()) as conn:
            pid = conn.execute("INSERT INTO research_projects(name,description) VALUES (?,?)",
                               (name.strip(), description.strip())).lastrowid
            conn.commit()
            return {"project": detail(conn, pid)}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: int, trial_page: int = Query(1, ge=1),
                    page_size: int = Query(100, ge=1, le=100),
                    watched_only: bool = False) -> dict:
        from core.projects import detail
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            return {"project": detail(conn, project_id, trial_page=trial_page,
                                      page_size=page_size, watched_only=watched_only)}

    @app.get("/api/projects/{project_id}/monitor-trials")
    def project_monitor_trials(project_id: int, page: int = Query(1, ge=1),
                               page_size: int = Query(100, ge=1, le=500)) -> dict:
        """Current linked-monitor matches, one row per trial across monitors."""
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            base = """FROM project_monitors pm JOIN monitors m ON m.id=pm.monitor_id
                JOIN monitor_trials mt ON mt.monitor_id=m.id AND mt.currently_matches=1"""
            total = conn.execute(f"SELECT COUNT(DISTINCT mt.trial_id) {base} WHERE pm.project_id=?", (project_id,)).fetchone()[0]
            rows = [dict(r) for r in conn.execute(f"""SELECT mt.trial_id, r.title, s.short_name source,
                GROUP_CONCAT(m.name, char(31)) monitor_names {base}
                LEFT JOIN registry_records r ON r.record_id=(
                    SELECT r2.record_id FROM registry_records r2 JOIN registry_sources s2 ON s2.source_id=r2.source_id
                    WHERE r2.source_trial_id=mt.trial_id AND r2.is_latest=1
                    ORDER BY CASE s2.short_name WHEN 'NCT' THEN 0 WHEN 'ChiCTR' THEN 1 WHEN 'CTR' THEN 2 WHEN 'CTIS' THEN 3 WHEN 'ISRCTN' THEN 4 WHEN 'EUCTR' THEN 5 WHEN 'ICTRP' THEN 6 ELSE 7 END, r2.record_id LIMIT 1)
                LEFT JOIN registry_sources s ON s.source_id=r.source_id
                WHERE pm.project_id=? GROUP BY mt.trial_id, r.title, s.short_name
                ORDER BY mt.trial_id LIMIT ? OFFSET ?""",
                (project_id, page_size, (page - 1) * page_size)).fetchall()]
            for row in rows:
                row["monitors"] = row.pop("monitor_names").split("\u001f")
            return {"total": total, "page": page, "page_size": page_size, "trials": rows}

    @app.get("/api/projects/{project_id}/export", response_class=PlainTextResponse)
    def export_project(project_id: int) -> PlainTextResponse:
        """Deterministic local Markdown; references persisted facts, no AI synthesis."""
        from core.projects import detail
        from core.intelligence import overview
        with closing(_open_conn()) as conn:
            p = detail(conn, project_id, unbounded=True)
            intel = overview(conn, scope="project", project_id=project_id, window="7d")
        lines = [f"# {p['name']}", "", p["description"], "", f"Generated: {_now()}",
                 f"Scope: project {project_id}; 7 days", "", "## Summary", "",
                 f"Curated trials: {p['counts']['trials']}",
                 f"Linked monitors: {p['counts']['monitors']}",
                 f"Saved searches: {p['counts']['saved_searches']}",
                 f"Change events: {intel['counts']['change_events']} (important {intel['counts']['important_changes']}, critical {intel['counts']['critical_changes']})", ""]
        for title, rows, render in (
            ("Saved Searches", p["saved_searches"], lambda x: f"{x['name']} (saved search #{x['id']})"),
            ("Monitors", p["monitors"], lambda x: f"{x['name']} (monitor #{x['id']}; {'enabled' if x['enabled'] else 'paused'})"),
            ("Curated Trials", p["trials"], lambda x: f"{x['source']}:{x['trial_id']} — {x['title'] or 'Untitled'}"),
            ("Important Changes", intel["priority_events"], lambda x: f"{x['source']}:{x['trial_id']} event #{x['event_id']} — {x['field_name']}: {x['old_value'] or '—'} → {x['new_value'] or '—'} ({x['severity']})"),
            ("Key Evidence", p["evidence"], lambda x: f"{x['source']}:{x['trial_id']} event #{x['event_id']} — {x['field_name']} ({x['severity']}){': ' + x['note'] if x['note'] else ''}"),
            ("Notes", p["notes"], lambda x: x["body"]),
            ("Source Freshness", intel["freshness"]["sources"], lambda x: f"{x['full_name']}: {x['freshness']}"),
        ):
            lines.extend([f"## {title}", ""])
            lines.extend(f"- {render(row)}" for row in rows)
            if not rows: lines.append("- None")
            lines.append("")
        if intel["freshness"]["affected"] or intel["freshness"]["unknown"]:
            lines.extend(["Freshness warning: relevant source data may be incomplete.", ""])
        return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8")

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: int, payload: Dict[str, Any] = Body(...)) -> dict:
        from core.projects import detail
        if not payload or set(payload) - {"name", "description", "pinned", "archived"}:
            raise HTTPException(status_code=422, detail="invalid project fields")
        updates, args = [], []
        for field in ("name", "description"):
            if field in payload:
                value = payload[field]
                if not isinstance(value, str) or (field == "name" and not value.strip()) or len(value) > (120 if field == "name" else 2000):
                    raise HTTPException(status_code=422, detail=f"invalid {field}")
                updates.append(f"{field}=?"); args.append(value.strip())
        for field in ("pinned", "archived"):
            if field in payload:
                if not isinstance(payload[field], bool):
                    raise HTTPException(status_code=422, detail=f"invalid {field}")
                if field == "archived":
                    updates.append("archived_at=" + ("datetime('now')" if payload[field] else "NULL"))
                else:
                    updates.append("pinned=?"); args.append(int(payload[field]))
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            conn.execute("UPDATE research_projects SET " + ",".join(updates) + ",updated_at=datetime('now') WHERE id=?", args + [project_id])
            conn.commit()
            return {"project": detail(conn, project_id)}

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: int) -> dict:
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            conn.execute("DELETE FROM research_projects WHERE id=?", (project_id,))
            conn.commit()
            return {"deleted": project_id}

    @app.post("/api/projects/{project_id}/assets/{kind}")
    def add_project_asset(project_id: int, kind: str, payload: Dict[str, Any] = Body(...)) -> dict:
        from core.projects import detail
        tables = {"searches": ("project_saved_searches", "saved_search_id", "saved_searches"),
                  "monitors": ("project_monitors", "monitor_id", "monitors")}
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            if kind == "trials":
                source, trial_id = payload.get("source"), payload.get("trial_id")
                if not isinstance(source, str) or not isinstance(trial_id, str) or not conn.execute("""SELECT 1 FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id
                    WHERE s.short_name=? AND r.source_trial_id=? AND r.is_latest=1""", (source, trial_id)).fetchone():
                    raise HTTPException(status_code=422, detail="trial not found")
                conn.execute("INSERT OR IGNORE INTO project_trials(project_id,source,trial_id) VALUES (?,?,?)", (project_id, source, trial_id))
            elif kind in tables:
                table, column, target = tables[kind]
                asset_id = payload.get("id")
                if not isinstance(asset_id, int) or isinstance(asset_id, bool) or not conn.execute(f"SELECT 1 FROM {target} WHERE id=?", (asset_id,)).fetchone():
                    raise HTTPException(status_code=422, detail="asset not found")
                conn.execute(f"INSERT OR IGNORE INTO {table}(project_id,{column}) VALUES (?,?)", (project_id, asset_id))
            else:
                raise HTTPException(status_code=404, detail="unknown asset type")
            conn.execute("UPDATE research_projects SET updated_at=datetime('now') WHERE id=?", (project_id,))
            conn.commit()
            return {"project": detail(conn, project_id)}

    @app.delete("/api/projects/{project_id}/assets/{kind}/{asset_id}")
    def remove_project_asset(project_id: int, kind: str, asset_id: str, source: Optional[str] = None) -> dict:
        from core.projects import detail
        tables = {"searches": ("project_saved_searches", "saved_search_id"),
                  "monitors": ("project_monitors", "monitor_id")}
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            if kind == "trials":
                if not source: raise HTTPException(status_code=422, detail="source required")
                conn.execute("DELETE FROM project_trials WHERE project_id=? AND source=? AND trial_id=?", (project_id, source, asset_id))
            elif kind in tables:
                table, column = tables[kind]
                if not asset_id.isdigit(): raise HTTPException(status_code=422, detail="invalid asset id")
                conn.execute(f"DELETE FROM {table} WHERE project_id=? AND {column}=?", (project_id, int(asset_id)))
            else:
                raise HTTPException(status_code=404, detail="unknown asset type")
            conn.execute("UPDATE research_projects SET updated_at=datetime('now') WHERE id=?", (project_id,))
            conn.commit()
            return {"project": detail(conn, project_id)}

    @app.post("/api/projects/{project_id}/notes", status_code=201)
    def add_project_note(project_id: int, payload: Dict[str, Any] = Body(...)) -> dict:
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 10000:
            raise HTTPException(status_code=422, detail="invalid note")
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            nid = conn.execute("INSERT INTO project_notes(project_id,body) VALUES (?,?)", (project_id, body.strip())).lastrowid
            conn.commit()
            return {"note": dict(conn.execute("SELECT * FROM project_notes WHERE id=?", (nid,)).fetchone())}

    @app.patch("/api/projects/{project_id}/notes/{note_id}")
    def update_project_note(project_id: int, note_id: int, payload: Dict[str, Any] = Body(...)) -> dict:
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 10000:
            raise HTTPException(status_code=422, detail="invalid note")
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            cur = conn.execute("UPDATE project_notes SET body=?,updated_at=datetime('now') WHERE id=? AND project_id=?", (body.strip(), note_id, project_id))
            if not cur.rowcount: raise HTTPException(status_code=404, detail="note not found")
            conn.commit()
            return {"note": dict(conn.execute("SELECT * FROM project_notes WHERE id=?", (note_id,)).fetchone())}

    @app.delete("/api/projects/{project_id}/notes/{note_id}")
    def delete_project_note(project_id: int, note_id: int) -> dict:
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            cur = conn.execute("DELETE FROM project_notes WHERE id=? AND project_id=?", (note_id, project_id))
            if not cur.rowcount: raise HTTPException(status_code=404, detail="note not found")
            conn.commit()
            return {"deleted": note_id}

    @app.post("/api/projects/{project_id}/evidence", status_code=201)
    def add_project_evidence(project_id: int, payload: Dict[str, Any] = Body(...)) -> dict:
        event_id, note = payload.get("event_id"), payload.get("note", "")
        if not isinstance(event_id, int) or isinstance(event_id, bool) or not isinstance(note, str) or len(note) > 2000:
            raise HTTPException(status_code=422, detail="invalid evidence")
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            if not conn.execute("SELECT 1 FROM trial_events WHERE event_id=?", (event_id,)).fetchone():
                raise HTTPException(status_code=422, detail="event not found")
            conn.execute("INSERT INTO project_evidence(project_id,event_id,note) VALUES (?,?,?) "
                         "ON CONFLICT(project_id,event_id) DO UPDATE SET note=excluded.note", (project_id, event_id, note.strip()))
            conn.commit()
            return {"evidence": dict(conn.execute("SELECT * FROM project_evidence WHERE project_id=? AND event_id=?", (project_id, event_id)).fetchone())}

    @app.delete("/api/projects/{project_id}/evidence/{event_id}")
    def remove_project_evidence(project_id: int, event_id: int) -> dict:
        with closing(_open_conn()) as conn:
            _project(conn, project_id)
            conn.execute("DELETE FROM project_evidence WHERE project_id=? AND event_id=?", (project_id, event_id))
            conn.commit()
            return {"deleted": event_id}

    # Saved searches are passive, single-user query configurations. None of
    # these handlers invoke matching_trials, run_monitor, or notification code.
    @app.get("/api/saved-searches")
    def list_saved_searches(q: str = "", pinned: Optional[bool] = None,
                            page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                            sort: str = Query("recent", pattern="^(recent|updated|name)$")) -> dict:
        from core.saved_searches import row_to_saved_search
        if len(q) > 200:
            raise HTTPException(status_code=422, detail="query too long")
        where, args = [], []
        if q.strip():
            where.append("(name LIKE ? OR description LIKE ? OR original_nl_query LIKE ?)")
            args.extend([f"%{q.strip()}%"] * 3)
        if pinned is not None:
            where.append("pinned=?")
            args.append(int(pinned))
        clause = " WHERE " + " AND ".join(where) if where else ""
        ordering = {"recent": "pinned DESC, COALESCE(last_opened_at, updated_at) DESC, id DESC",
                    "updated": "pinned DESC, updated_at DESC, id DESC",
                    "name": "pinned DESC, name COLLATE NOCASE, id DESC"}[sort]
        with closing(_open_conn()) as conn:
            total = conn.execute("SELECT count(*) FROM saved_searches" + clause, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM saved_searches" + clause + " ORDER BY " + ordering + " LIMIT ? OFFSET ?",
                                [*args, page_size, (page - 1) * page_size]).fetchall()
        try:
            items = [row_to_saved_search(row) for row in rows]
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.post("/api/saved-searches", status_code=201)
    def create_saved_search(payload: Dict[str, Any] = Body(...)) -> dict:
        import json
        from core.saved_searches import validate_metadata, row_to_saved_search
        try:
            data = validate_metadata(payload, create=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with closing(_open_conn()) as conn:
            sid = conn.execute("""INSERT INTO saved_searches
                (name,description,state_json,original_nl_query,interpreter_version,pinned)
                VALUES (?,?,?,?,?,?)""", (data["name"], data.get("description"),
                 json.dumps(data["state"], ensure_ascii=False), data.get("original_nl_query"),
                 data.get("interpreter_version"), data.get("pinned", 0))).lastrowid
            conn.commit()
            row = conn.execute("SELECT * FROM saved_searches WHERE id=?", (sid,)).fetchone()
            return {"saved_search": row_to_saved_search(row)}

    def _saved_row(conn: sqlite3.Connection, saved_id: int):
        row = conn.execute("SELECT * FROM saved_searches WHERE id=?", (saved_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="saved search not found")
        return row

    @app.get("/api/saved-searches/{saved_id}")
    def get_saved_search(saved_id: int) -> dict:
        from core.saved_searches import row_to_saved_search
        with closing(_open_conn()) as conn:
            try:
                return {"saved_search": row_to_saved_search(_saved_row(conn, saved_id))}
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/saved-searches/{saved_id}/open")
    def open_saved_search(saved_id: int) -> dict:
        from core.saved_searches import row_to_saved_search
        with closing(_open_conn()) as conn:
            row = _saved_row(conn, saved_id)
            try:
                row_to_saved_search(row)  # reject incompatible stored state before mutating
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            conn.execute("UPDATE saved_searches SET last_opened_at=datetime('now') WHERE id=?", (saved_id,))
            conn.commit()
            return {"saved_search": row_to_saved_search(_saved_row(conn, saved_id))}

    @app.patch("/api/saved-searches/{saved_id}")
    def update_saved_search(saved_id: int, payload: Dict[str, Any] = Body(...)) -> dict:
        import json
        from core.saved_searches import validate_metadata, row_to_saved_search
        try:
            data = validate_metadata(payload, create=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        columns = {"state": "state_json", **{k: k for k in data if k != "state"}}
        updates = [f"{columns[key]}=?" for key in data]
        values = [json.dumps(value, ensure_ascii=False) if key == "state" else value
                  for key, value in data.items()]
        with closing(_open_conn()) as conn:
            _saved_row(conn, saved_id)
            conn.execute("UPDATE saved_searches SET " + ", ".join(updates) +
                         ", updated_at=datetime('now') WHERE id=?", [*values, saved_id])
            conn.commit()
            return {"saved_search": row_to_saved_search(_saved_row(conn, saved_id))}

    @app.delete("/api/saved-searches/{saved_id}")
    def delete_saved_search(saved_id: int) -> dict:
        with closing(_open_conn()) as conn:
            _saved_row(conn, saved_id)
            conn.execute("DELETE FROM saved_searches WHERE id=?", (saved_id,))
            conn.commit()
        return {"deleted": saved_id}

    # ── 实时查原站（live-check，tier1 查漏回源 / tier3 同步透传共用） ─────

    # 同一 (source, q) 的会话缓存：交互式重复检索不产生额外 WAF 压力
    # （WAF 红线：短时间重复爬取会触发 path 级 405）。
    # 会话记录已取条目与翻页游标，供「加载更多」分批续抓：
    #   · 首次点击 ChiCTR/NCT ≤5 页、CTR ≤3 页，且到原站总数即停；
    #   · continue 续批每次 ≤2 页，批间强制 ≥10s（不靠前端自觉）；
    #   · 响应只保留前 200 条预览，但会话会持续翻页到原站末页；
    #   · 5 分钟内重复整查仍走缓存；共享熔断器兜底。
    _LIVE_CHECK_COOLDOWN_SEC = 300
    _LIVE_CHECK_BURST_INTERVAL_SEC = 10
    _LIVE_CHECK_BURST_PAGES = 2
    _LIVE_CHECK_PREVIEW_CAP = 200
    _LIVE_CHECK_INITIAL_PAGE_CAP = {
        "chictr": 5, "ctr": 3, "nct": 5, "ictrp": 5,
    }
    # 整串 0 命中拆词回退：最多补查的段数（每段 1 页，且 1+段数 ≤ 首查页上限）
    _LIVE_CHECK_SEGMENT_CAP = 3
    _live_check_cache: Dict[str, dict] = {}
    _live_collectors: Dict[str, Any] = {}
    _chictr_api_cache: Dict[Any, tuple] = {}
    _chictr_api_state = {"last_outbound": 0.0}
    _chictr_api_lock = threading.Lock()
    _ctr_api_cache: Dict[Any, tuple] = {}
    _ctr_api_state = {"last_outbound": 0.0}
    _ctr_api_lock = threading.Lock()
    _ictrp_api_cache: Dict[Any, tuple] = {}
    _ictrp_api_state = {"last_outbound": 0.0}
    _ictrp_api_lock = threading.Lock()

    def _live_collector(key: str):
        """进程内单例采集器（熔断器状态跨请求保留；构造不启浏览器）。"""
        if key not in _live_collectors:
            if key == "chictr":
                from collectors.chictr import ChiCTRCollector
                _live_collectors[key] = ChiCTRCollector()
            elif key == "nct":
                from collectors.clinicaltrials import ClinicalTrialsGovCollector
                _live_collectors[key] = ClinicalTrialsGovCollector()
            elif key == "ctr":
                from collectors.chinadrugtrials import ChinaDrugTrialsCollector
                _live_collectors[key] = ChinaDrugTrialsCollector()
            elif key == "ictrp":
                from collectors.who_ictrp import WHOICTRPCollector
                _live_collectors[key] = WHOICTRPCollector()
            else:  # pragma: no cover - callers use the closed key set above
                raise KeyError(f"unknown live source: {key}")
        return _live_collectors[key]

    def _live_entry_payload(key: str, collector, session: dict,
                            status: str, **extra) -> dict:
        """会话 → 响应条目：in_library 批量标注 + 累计统计（不抓原站）。"""
        entries = session["entries"]
        ids = [e["source_trial_id"] for e in entries]
        in_lib: set = set()
        with closing(_open_conn()) as conn:
            for i in range(0, len(ids), 200):  # SQLite 变量上限内分批
                chunk = ids[i:i + 200]
                marks = conn.execute(
                    "SELECT source_trial_id FROM registry_records "
                    "WHERE source_id = ? AND source_trial_id IN "
                    f"({','.join('?' * len(chunk))}) AND is_latest = 1",
                    [collector.source_id, *chunk],
                ).fetchall()
                in_lib.update(r["source_trial_id"] for r in marks)
        for e in entries:
            e["in_library"] = e["source_trial_id"] in in_lib
        entry = {
            "source": key, "status": status,
            "found": session.get("found", len(entries)), "shown": len(entries),
            "queued": session["queued"], "enriched": session["enriched"],
            "pages_walked": session["pages_walked"],
            "stopped_reason": session.get("stopped_reason"),
            "site_total": session.get("site_total"),
            "has_more": session["has_more"],
            "query_used": session.get("query_used"),
            "field_used": session.get("field_used"),
            "entries": entries,
        }
        entry.update(extra)
        return entry

    def _live_segment_fallback(collector, segments, source_max_pages):
        """整串 0 命中 → 拆词逐段检索（每段 1 页），OR 合并、全段命中优先。

        只在原样整串检索 0 命中时触发：中文复合词（如“结直肠癌甲基化”）
        在原站标题索引里通常不是连续子串，拆出的词各自才能命中。段数与
        页数受首查预算约束（1 + 段数 ≤ 首查页上限）；回退批不提供续抓
        （has_more=False，多查询词的会话游标没有单一含义）。返回
        (merged_stats, used_segments)，未执行或全部段失败返回 None。
        """
        cap = min(_LIVE_CHECK_SEGMENT_CAP, max(source_max_pages - 1, 1))
        merged: list = []
        seen: set = set()
        used: list = []
        queued = 0
        for seg in segments[:cap]:
            try:
                seg_stats = collector.live_search(seg, max_pages=1)
            except WafCircuitOpenError:
                raise
            except Exception as exc:
                logger.warning("live-check segment search '%s' failed: %s",
                               seg, exc)
                break
            used.append(seg)
            queued += seg_stats.get("queued", 0)
            for e in seg_stats.get("entries", []):
                if e["source_trial_id"] in seen:
                    continue
                seen.add(e["source_trial_id"])
                merged.append(e)
        if not used:
            return None
        # 命中全部段的排前面（stable：同层内保持段序）
        used_lower = [s.lower() for s in used]
        merged.sort(key=lambda e: 0 if all(
            s in (e.get("title") or "").lower() for s in used_lower) else 1)
        stats = {
            "keyword": " ".join(used), "found": len(merged),
            "queued": queued, "pages_walked": 1 + len(used),
            "stopped_reason": "segmented_fallback",
            "site_total": None, "has_more": False,
            "next_page": None, "next_token": None,
            "entries": merged,
        }
        return stats, used

    def _chictr_api_request(key: Any,
                            loader: Callable[[], Optional[dict]]) -> dict:
        """Run one cached, rate-limited ChiCTR adapter request.

        The lock covers the outbound call intentionally: one local server
        process must never stack concurrent requests against the WAF-protected
        registry.  Cached responses do not consume the outbound interval.
        """
        with _chictr_api_lock:
            now = time.monotonic()
            hit = _chictr_api_cache.get(key)
            if hit and now - hit[0] < CHICTR_API_TTL_SEC:
                return {**hit[1], "cached": True}

            wait = CHICTR_API_MIN_INTERVAL_SEC - (
                now - _chictr_api_state["last_outbound"]
            )
            if wait > 0:
                time.sleep(wait)
            _chictr_api_state["last_outbound"] = time.monotonic()
            payload = loader()
            if payload is None:
                payload = {"not_found": True}
            if len(_chictr_api_cache) >= 128:
                _chictr_api_cache.clear()
            _chictr_api_cache[key] = (time.monotonic(), payload)
            return {**payload, "cached": False}

    def _ictrp_api_request(key: Any,
                           loader: Callable[[], dict]) -> dict:
        """Run one cached, serialized WHO Search Portal request."""
        with _ictrp_api_lock:
            now = time.monotonic()
            hit = _ictrp_api_cache.get(key)
            if hit and now - hit[0] < ICTRP_API_TTL_SEC:
                return {**hit[1], "cached": True}
            wait = ICTRP_API_MIN_INTERVAL_SEC - (
                now - _ictrp_api_state["last_outbound"]
            )
            if wait > 0:
                time.sleep(wait)
            _ictrp_api_state["last_outbound"] = time.monotonic()
            payload = loader()
            if len(_ictrp_api_cache) >= 128:
                _ictrp_api_cache.clear()
            _ictrp_api_cache[key] = (time.monotonic(), payload)
            return {**payload, "cached": False}

    def _ctr_api_request(key: Any,
                         loader: Callable[[], Optional[dict]]) -> dict:
        """Run one cached, rate-limited China Drug Trials adapter call."""
        with _ctr_api_lock:
            now = time.monotonic()
            hit = _ctr_api_cache.get(key)
            if hit and now - hit[0] < CTR_API_TTL_SEC:
                return {**hit[1], "cached": True}
            wait = CTR_API_MIN_INTERVAL_SEC - (
                now - _ctr_api_state["last_outbound"]
            )
            if wait > 0:
                time.sleep(wait)
            _ctr_api_state["last_outbound"] = time.monotonic()
            payload = loader()
            if payload is None:
                payload = {"not_found": True}
            if len(_ctr_api_cache) >= 128:
                _ctr_api_cache.clear()
            _ctr_api_cache[key] = (time.monotonic(), payload)
            return {**payload, "cached": False}

    def _ictrp_live_request(loader: Callable[[], dict]) -> dict:
        """Serialize an uncached live-check call against the WHO portal."""
        with _ictrp_api_lock:
            now = time.monotonic()
            wait = ICTRP_API_MIN_INTERVAL_SEC - (
                now - _ictrp_api_state["last_outbound"]
            )
            if wait > 0:
                time.sleep(wait)
            _ictrp_api_state["last_outbound"] = time.monotonic()
            return loader()

    @app.get("/api/chictr/search")
    def chictr_search(
        q: str = Query(..., min_length=1, max_length=200,
                       description="Keyword sent to ChiCTR's title search"),
        page: int = Query(1, ge=1, le=1000),
    ) -> dict:
        """Read-only local API over one ChiCTR search-results page."""
        query = q.strip()
        if not query:
            raise HTTPException(status_code=422, detail="q is blank")
        collector = _live_collector("chictr")
        try:
            result = _chictr_api_request(
                ("search", query.lower(), page),
                lambda: collector.api_search(query, page),
            )
        except WafCircuitOpenError as exc:
            raise HTTPException(status_code=503,
                                detail=f"ChiCTR circuit is open: {exc}")
        except Exception as exc:
            logger.warning("ChiCTR API search failed q=%r page=%d: %s",
                           query, page, exc)
            raise HTTPException(status_code=502,
                                detail=f"ChiCTR request failed: {exc}")
        return {"generated_at": _now(), "live": True, **result}

    @app.get("/api/chictr/studies/{registration_id}")
    def chictr_study(registration_id: str) -> dict:
        """Read-only local API for a current ChiCTR detail record."""
        trial_id = registration_id.strip()
        if not re.fullmatch(r"ChiCTR\d{10,}", trial_id, flags=re.IGNORECASE):
            raise HTTPException(
                status_code=422,
                detail=f"not a ChiCTR registration number: {trial_id}",
            )
        trial_id = "ChiCTR" + trial_id[6:]
        collector = _live_collector("chictr")
        try:
            result = _chictr_api_request(
                ("study", trial_id.lower()),
                lambda: collector.api_study(trial_id),
            )
        except WafCircuitOpenError as exc:
            raise HTTPException(status_code=503,
                                detail=f"ChiCTR circuit is open: {exc}")
        except Exception as exc:
            logger.warning("ChiCTR API detail failed id=%s: %s", trial_id, exc)
            raise HTTPException(status_code=502,
                                detail=f"ChiCTR request failed: {exc}")
        if result.get("not_found"):
            raise HTTPException(status_code=404,
                                detail=f"{trial_id} not found on ChiCTR")
        return {"generated_at": _now(), "live": True, **result}

    @app.get("/api/ictrp/search")
    def ictrp_search(
        q: str = Query(..., min_length=1, max_length=200,
                       description="English query sent to WHO ICTRP"),
        page: int = Query(1, ge=1, le=1000),
    ) -> dict:
        """Private, cached API over one WHO ICTRP Search Portal page."""
        query = q.strip()
        if not query:
            raise HTTPException(status_code=422, detail="q is blank")
        from core import terminology
        with closing(_open_conn()) as conn:
            translated = (terminology.lookup_learned(conn, query)
                          or terminology.translate(query))
            segmented = terminology.translate_segments(query, conn)
        source_query = query
        if translated and not terminology.has_cjk(translated):
            source_query = translated
        elif segmented:
            source_query = segmented
        collector = _live_collector("ictrp")
        try:
            result = _ictrp_api_request(
                (source_query.casefold(), page),
                lambda: collector.live_search(
                    source_query, max_pages=1, start_page=page),
            )
        except Exception as exc:
            logger.warning("ICTRP API search failed q=%r page=%d: %s",
                           source_query, page, exc)
            raise HTTPException(status_code=502,
                                detail=f"WHO ICTRP request failed: {exc}")
        return {
            "generated_at": _now(), "live": True,
            "query": query, "query_used": source_query, "page": page,
            **result,
        }

    @app.get("/api/ictrp/studies/{registration_id:path}")
    def ictrp_study(registration_id: str) -> dict:
        """Return the latest locally mirrored WHO ICTRP record.

        Unlike ``/api/ictrp/search``, this endpoint never contacts WHO.  It
        exposes the lossless payload already imported from an ICTRP XML
        export, or identifies a portal-live placeholder as list metadata so a
        client cannot mistake it for a complete export record.
        """
        import json

        trial_id = registration_id.strip()
        if not trial_id or len(trial_id) > 200 or any(
                ord(char) < 32 for char in trial_id):
            raise HTTPException(
                status_code=422,
                detail=f"invalid ICTRP registration number: {trial_id}",
            )

        with closing(_open_conn()) as conn:
            row = conn.execute(
                """SELECT r.*, st.label AS status,
                          typ.label AS study_type
                   FROM registry_records r
                   JOIN registry_sources src ON src.source_id = r.source_id
                   LEFT JOIN status_types st ON st.status_type_id = r.status_id
                   LEFT JOIN study_types typ
                          ON typ.study_type_id = r.study_type_id
                   WHERE src.short_name = 'ICTRP' AND r.is_latest = 1
                         AND lower(r.source_trial_id) = lower(?)
                   ORDER BY r.version_number DESC LIMIT 1""",
                (trial_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"{trial_id} is not present in the local ICTRP mirror",
                )
            data_as_of = _data_as_of(conn).get("ICTRP")

        record = dict(row)
        raw_text = record.pop("raw_payload", None)
        try:
            raw_payload = json.loads(raw_text) if raw_text else None
        except (TypeError, ValueError):
            # Preserve a legacy/non-JSON payload losslessly instead of hiding
            # it or pretending it is a parsed WHO record.
            raw_payload = raw_text

        who_record = (
            raw_payload.get("who_ictrp_record", {})
            if isinstance(raw_payload, dict) else {}
        )
        portal_only = bool(who_record.get("_portal_live"))
        xml_export_record = bool(who_record) and not portal_only

        for name in (
            "conditions", "interventions", "countries", "locations",
            "sponsors", "secondary_endpoints", "arm_group_interventions",
        ):
            value = record.get(name)
            if not isinstance(value, str):
                continue
            try:
                record[name] = json.loads(value)
            except (TypeError, ValueError):
                # Older rows may contain a plain scalar; returning it is more
                # faithful than manufacturing an array shape.
                pass

        for internal in ("source_id", "status_id", "study_type_id",
                         "data_hash", "superseded_by"):
            record.pop(internal, None)

        return {
            "generated_at": _now(),
            "data_as_of": data_as_of,
            "live": False,
            "source": "ICTRP",
            "source_trial_id": record["source_trial_id"],
            "record_scope": (
                "portal_list_metadata" if portal_only
                else "xml_export_record" if xml_export_record
                else "legacy_unknown"
            ),
            "complete_export_record": xml_export_record,
            "canonical": record,
            "raw_payload": raw_payload,
        }

    @app.get("/api/ctr/search")
    def ctr_search(
        q: str = Query(..., min_length=1, max_length=200,
                       description="Query sent to China Drug Trials/CDE"),
        page: int = Query(1, ge=1, le=1000),
        field: Optional[str] = Query(
            None, pattern="^(keywords|indication)$",
            description="Optional upstream search field override",
        ),
    ) -> dict:
        """Read-only local API over one China Drug Trials result page."""
        query = q.strip()
        if not query:
            raise HTTPException(status_code=422, detail="q is blank")
        collector = _live_collector("ctr")
        try:
            result = _ctr_api_request(
                ("search", query.casefold(), page, field),
                lambda: collector.api_search(query, page, field=field),
            )
        except WafCircuitOpenError as exc:
            raise HTTPException(status_code=503,
                                detail=f"CTR circuit is open: {exc}")
        except Exception as exc:
            logger.warning("CTR API search failed q=%r page=%d: %s",
                           query, page, exc)
            raise HTTPException(status_code=502,
                                detail=f"CTR request failed: {exc}")
        return {"generated_at": _now(), "live": True, **result}

    @app.get("/api/ctr/studies/{registration_id}")
    def ctr_study(registration_id: str) -> dict:
        """Read-only local API for a current CDE/CTR detail record."""
        trial_id = registration_id.strip().upper()
        if not re.fullmatch(r"CTR\d{8,}", trial_id):
            raise HTTPException(
                status_code=422,
                detail=f"not a CTR registration number: {trial_id}",
            )
        collector = _live_collector("ctr")
        try:
            result = _ctr_api_request(
                ("study", trial_id),
                lambda: collector.api_study(trial_id),
            )
        except WafCircuitOpenError as exc:
            raise HTTPException(status_code=503,
                                detail=f"CTR circuit is open: {exc}")
        except Exception as exc:
            logger.warning("CTR API detail failed id=%s: %s", trial_id, exc)
            raise HTTPException(status_code=502,
                                detail=f"CTR request failed: {exc}")
        if result.get("not_found"):
            raise HTTPException(status_code=404,
                                detail=f"{trial_id} not found on CTR")
        return {"generated_at": _now(), "live": True, **result}

    @app.post("/api/trials/live-check")
    def live_check_trials(payload: Dict[str, Any] = Body(...)) -> dict:
        """同步查注册库原站（ChiCTR/CTR/WHO 门户适配器 + NCT 官方 API）。
        命中行入库/入补爬队列后返回规范化命中；WHO 只返回轻量列表，
        完整字段仍由每周 XML 快照入库。

        原站压力受多重约束：首次 ChiCTR/NCT ≤5 列表页、CTR ≤3 页，且
        到原站总数即停；``continue`` 续批每次 ≤2 页、批间强制 ≥10s、
        响应保留前 200 条预览，但全量模式会沿游标持续抓到末页；
        (source, q) 冷却缓存（5 分钟内整查走缓存）；
        进程级共享熔断器兜底。NCT 为官方 API，按 pageToken 同样分批。
        """
        q = str(payload.get("q") or "").strip()[:200]
        if not q:
            raise HTTPException(status_code=422, detail="q is required")
        try:
            max_pages = min(max(int(payload.get("max_pages") or 5), 1), 5)
            enrich_limit = min(max(int(payload.get("enrich_limit") or 6), 0), 10)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="max_pages/enrich_limit must be integers")
        continue_burst = bool(payload.get("continue"))
        full_ingest = bool(payload.get("full_ingest"))
        wanted = payload.get("sources") or ["chictr", "ctr", "nct", "ictrp"]
        if isinstance(wanted, str):
            wanted = [wanted]
        keys = [k for k in ("chictr", "ctr", "nct", "ictrp") if k in wanted]

        # 双语分发：各注册库索引语言不同（NCT 全英文、CTR 中文为主、
        # ChiCTR 双语）。术语表（静态 + 已学词条）能翻译时给每个源发它
        # 索引能理解的变体，一次检索同时命中中英文；查不到翻译则原词直发。
        # 中文复合词（无空格）另做拆词：整串 0 命中时拆段回退（WAF 源）、
        # 分段译文给 NCT（"结直肠癌甲基化" → "colorectal cancer methylation"）。
        from core import terminology
        from core.waf_guard import WafCircuitOpenError
        with closing(_open_conn()) as conn:
            variant = (terminology.lookup_learned(conn, q)
                       or terminology.translate(q))
            segments = terminology.segment(q, conn)
            seg_en = terminology.translate_segments(q, conn)

        def query_for_source(key: str) -> str:
            if key in ("nct", "ictrp"):
                # NCT 全英文：整词译文优先，翻不出再试分段译文（空格即
                # AND），都翻不出才原词直发；ICTRP 门户同样只索引英文。
                if variant and not terminology.has_cjk(variant):
                    return variant
                if seg_en:
                    return seg_en
                return q
            if not variant or variant.lower() == q.lower():
                return q
            if key == "ctr" and terminology.has_cjk(variant):
                return variant
            return q

        results: List[Dict[str, Any]] = []
        for key in keys:
            cache_key = key + ":" + q.lower()
            session = _live_check_cache.get(cache_key)
            now = time.time()
            collector = _live_collector(key)

            # ── 续批：会话存在且仍有后续页 ────────────────────────────
            if continue_burst and session and session["has_more"]:
                wait = (
                    _LIVE_CHECK_BURST_INTERVAL_SEC
                    - (now - session["last_fetch"])
                    if key in ("chictr", "ctr") else 0
                )
                if wait > 0:
                    results.append(_live_entry_payload(
                        key, collector, session, "cooldown",
                        retry_after=int(wait) + 1))
                    continue
                source_query = session.get("query_used") or q
                try:
                    if key == "nct":
                        stats = collector.live_search(
                            source_query, max_pages=_LIVE_CHECK_BURST_PAGES,
                            start_token=session.get("next_token"))
                    elif key == "ictrp":
                        stats = _ictrp_live_request(lambda: collector.live_search(
                            source_query, max_pages=_LIVE_CHECK_BURST_PAGES,
                            start_page=session.get("next_page") or 1))
                    else:
                        stats = collector.live_search(
                            source_query, max_pages=_LIVE_CHECK_BURST_PAGES,
                            start_page=session.get("next_page") or 1,
                            field=session.get("field_used"))
                except WafCircuitOpenError:
                    results.append(_live_entry_payload(
                        key, collector, session, "circuit_open"))
                    continue
                except Exception as exc:
                    logger.warning("live-check continue %s '%s' failed: %s",
                                   key, q, exc)
                    results.append(_live_entry_payload(
                        key, collector, session, "error",
                        error=str(exc)[:300]))
                    continue
                seen = session.setdefault(
                    "seen_ids",
                    {e["source_trial_id"] for e in session["entries"]},
                )
                fresh = [e for e in stats["entries"]
                         if e["source_trial_id"] not in seen]
                seen.update(e["source_trial_id"] for e in fresh)
                preview_room = max(
                    _LIVE_CHECK_PREVIEW_CAP - len(session["entries"]), 0)
                session["entries"].extend(fresh[:preview_room])
                session["found"] = len(seen)
                session["queued"] += stats.get("queued", 0)
                batch_enriched = stats.get("enriched", 0)
                if full_ingest and fresh:
                    if key in ("chictr", "ctr") and hasattr(
                            collector, "enrich_pending"):
                        enrich_stats = collector.enrich_pending(
                            limit=len(fresh), keywords=[source_query],
                            source_trial_ids=[e["source_trial_id"] for e in fresh],
                            workers=2)
                        batch_enriched = enrich_stats.get("enriched", 0)
                    elif key == "ictrp" and hasattr(
                            collector, "ingest_live_entries"):
                        batch_enriched = collector.ingest_live_entries(fresh)
                session["enriched"] += batch_enriched
                session["pages_walked"] += stats.get("pages_walked", 0)
                session["next_page"] = stats.get("next_page")
                session["next_token"] = stats.get("next_token")
                session["site_total"] = stats.get("site_total") or session.get("site_total")
                session["stopped_reason"] = stats.get("stopped_reason")
                session["has_more"] = bool(stats.get("has_more"))
                session["last_fetch"] = time.time()
                results.append(_live_entry_payload(
                    key, collector, session, "ok"))
                continue

            # ── 整查：5 分钟内重复 → 缓存会话原样返回 ─────────────────
            cache_ttl = (ICTRP_API_TTL_SEC if key == "ictrp"
                         else _LIVE_CHECK_COOLDOWN_SEC)
            if session and now - session["last_fetch"] < cache_ttl:
                results.append(_live_entry_payload(key, collector, session,
                                                   "cached"))
                continue
            # 续批请求但无会话/无后续页 → 按整查处理（会话缺失或已完结）

            entry: Dict[str, Any] = {"source": key}
            source_query = query_for_source(key)
            try:
                source_max_pages = min(
                    max_pages, _LIVE_CHECK_INITIAL_PAGE_CAP.get(key, 3))
                if key == "ictrp":
                    stats = _ictrp_live_request(lambda: collector.live_search(
                        source_query, max_pages=source_max_pages))
                else:
                    stats = collector.live_search(
                        source_query, max_pages=source_max_pages)
                # 中文复合词整串 0 命中 → 拆词逐段回退（WAF 源；NCT 走
                # 分段译文，无此需要）
                seg_used = None
                if key in ("chictr", "ctr") and segments and not stats["entries"]:
                    fallback = _live_segment_fallback(
                        collector, segments, source_max_pages)
                    if fallback:
                        stats, seg_used = fallback
                enriched = stats.get("enriched", 0)
                effective_enrich_limit = (
                    len(stats.get("entries", [])) if full_ingest
                    else enrich_limit
                )
                if (effective_enrich_limit and key in ("chictr", "ctr")
                        and hasattr(collector, "enrich_pending")):
                    # WAF 源：命中的新号入队，小预算即时增强；
                    # 拆词回退时按段做标题关键词优先（整串 LIKE 必然 0 命中）；
                    # NCT：live_search 已直接 upsert，无 enrich_pending
                    enrich_kwargs = {
                        "limit": effective_enrich_limit,
                        "keywords": seg_used or [source_query],
                        "workers": 2,
                    }
                    if full_ingest:
                        enrich_kwargs["source_trial_ids"] = [
                            e["source_trial_id"] for e in stats["entries"]]
                    enrich_stats = collector.enrich_pending(**enrich_kwargs)
                    enriched = enrich_stats.get("enriched", 0)
                elif (full_ingest and key == "ictrp"
                      and hasattr(collector, "ingest_live_entries")):
                    enriched = collector.ingest_live_entries(stats["entries"])
                all_initial_entries = stats["entries"]
                initial_entries = all_initial_entries[:_LIVE_CHECK_PREVIEW_CAP]
                seen_ids = {e["source_trial_id"] for e in all_initial_entries}
                session = {
                    "entries": initial_entries,
                    "seen_ids": seen_ids,
                    "found": len(seen_ids),
                    "queued": stats.get("queued", 0),
                    "enriched": enriched,
                    "pages_walked": stats.get("pages_walked", 0),
                    "next_page": stats.get("next_page"),
                    "next_token": stats.get("next_token"),
                    "site_total": stats.get("site_total"),
                    "stopped_reason": stats.get("stopped_reason"),
                    "has_more": bool(stats.get("has_more")),
                    "query_used": (" ".join(seg_used) if seg_used
                                   else source_query),
                    # 拆词回退时各段字段各异（疾病段走 indication），整串
                    # 字段标签失去含义 → 置 None 让前端不显示
                    "field_used": None if seg_used
                    else stats.get("field_used"),
                    "last_fetch": time.time(),
                }
                _live_check_cache[cache_key] = session
                results.append(_live_entry_payload(key, collector, session,
                                                   "ok"))
            except WafCircuitOpenError:
                if session:
                    results.append(_live_entry_payload(
                        key, collector, session, "circuit_open"))
                else:
                    entry.update({"status": "circuit_open", "found": 0,
                                  "queued": 0, "enriched": 0, "entries": []})
                    results.append(entry)
            except Exception as exc:
                logger.warning("live-check %s '%s' failed: %s", key, q, exc)
                entry.update({"status": "error", "error": str(exc)[:300],
                              "found": 0, "queued": 0, "enriched": 0,
                              "entries": []})
                results.append(entry)
        return {"generated_at": _now(), "q": q, "results": results}

    @app.get("/api/trials/search")
    def search_trials(
        q: str = Query("", max_length=500, description="Shared monitor query text"),
        registries: str = Query("", max_length=1000, description="Comma-separated registry short names"),
        statuses: str = Query("", max_length=1000, description="Comma-separated normalized statuses"),
        study_types: str = Query("", max_length=1000, description="Comma-separated study types"),
        phase: str = Query("", max_length=1000, description="Comma-separated phases"),
        country: str = Query("", max_length=1000, description="Comma-separated countries"),
        sponsor: str = Query("", max_length=1000, description="Comma-separated sponsors"),
        condition: str = Query("", max_length=1000, description="Comma-separated conditions"),
        intervention: str = Query("", max_length=1000, description="Comma-separated interventions"),
        sort: str = Query("last_updated", pattern="^(last_updated|start_date|id)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
    ) -> dict:
        """Read-only cross-registry discovery using monitor-rule semantics.

        Facets are calculated before pagination, so counts describe the full
        matching set rather than just the currently displayed page.
        """
        from core.monitors import matching_trials, normalize_phase

        def values(value: str) -> List[str]:
            from urllib.parse import unquote
            return [unquote(item.strip()) for item in value.split(",") if item.strip()]

        rules = {
            "query": q.strip(), "registries": values(registries),
            "statuses": values(statuses), "study_types": values(study_types),
            "phase": values(phase), "country": values(country),
            "sponsor": values(sponsor), "condition": values(condition),
            "intervention": values(intervention),
        }
        # Empty fields are omitted so a URL round-trip creates the exact same
        # compact rule that is persisted when a user tracks this search.
        rules = {key: value for key, value in rules.items() if value}
        # 双语扩展（core.terminology）：查询词恰为术语表词条（静态或已学）
        # 时，把译文作为 OR 分支合并——本地库中英混存（NCT 英文、ChiCTR/CTR
        # 中文），单语言词只能命中一半。扩展只发生在检索端点；
        # matching_trials 本体不动，saved monitor 仍按保存时的原词求值。
        from core import terminology
        q_clean = q.strip()

        # Reopening a search (page reload, saved search, shared link) must
        # not re-run matching + bilingual expansion + facets every time:
        # memoize the payload per normalized rule set.  _DataCache drops the
        # entry as soon as any connection commits (pipeline sync, terminology
        # learning), so freshness is bounded by actual data change, not a TTL.
        # Pagination is presentation, not part of the expensive query.  Cache
        # the complete ordered result set once per rule+sort and slice it for
        # each page below.  This is especially important for CSV export, which
        # walks several pages: previously every page repeated FTS matching,
        # bilingual expansion, shaping and facet calculation from scratch.
        rule_key = ("search-result-set",
                    tuple(sorted((key, value if isinstance(value, str) else tuple(value))
                                 for key, value in rules.items())),
                    sort)

        def build() -> dict:
            with closing(_open_conn()) as conn:
                alt_query = (terminology.lookup_learned(conn, q_clean)
                             or terminology.translate(q_clean))
                if alt_query and alt_query.lower() == q_clean.lower():
                    alt_query = None
                rows = matching_trials(conn, rules)
                if alt_query:
                    alt_rows = matching_trials(conn, {**rules, "query": alt_query})
                    seen_keys = set()
                    merged = []
                    for r in list(rows) + list(alt_rows):
                        k = (r["short_name"], r["source_trial_id"])
                        if k not in seen_keys:
                            seen_keys.add(k)
                            merged.append(r)
                    rows = merged
                else:
                    # 无译文可用的词：从同注册号跨库兄弟记录学习候选译词
                    # （evidence≥2 自动生效，下次检索起参与扩展）
                    terminology.learn_from_results(conn, q_clean)
                # 中文复合词 0 命中 → 拆词 OR 扩展（各段含译文也并入）。
                # 动机与 live-check 拆词回退一致：“结直肠癌甲基化”整串在本地
                # 子串匹配里是 0 命中，拆出的段各自命中。与双语扩展同界：
                # 只发生在检索端点，matching_trials 本体不动，saved monitor
                # 仍按保存时原词精确求值。
                segmented = None
                if not rows and q_clean:
                    segs = terminology.segment(q_clean, conn)
                    if segs:
                        seen_keys: set = set()
                        seg_rows: list = []
                        for seg in segs:
                            kws = [seg]
                            seg_alt = (terminology.lookup_learned(conn, seg)
                                       or terminology.translate(seg))
                            if seg_alt and seg_alt.lower() != seg.lower():
                                kws.append(seg_alt)
                            for kw in kws:
                                for r in matching_trials(conn,
                                                         {**rules, "query": kw}):
                                    k = (r["short_name"], r["source_trial_id"])
                                    if k not in seen_keys:
                                        seen_keys.add(k)
                                        seg_rows.append(r)
                        if seg_rows:
                            rows = list(rows) + seg_rows
                            segmented = segs
                items = [build_light_trial(row) for row in rows]
                facets: Dict[str, Any] = {"registries": {}, "statuses": {}, "phases": {}, "countries": {}}
                for item in items:
                    def count(group: str, value: Any) -> None:
                        if value:
                            facets[group][str(value)] = facets[group].get(str(value), 0) + 1
                    count("registries", item.get("source"))
                    count("statuses", item.get("status"))
                    # Facet keys are canonical buckets (normalize_phase), not
                    # raw study_phase strings — ICTRP combination phases would
                    # otherwise fragment into one-count chips.  The phase rule
                    # matcher compares the same buckets (core.monitors._phase_ok).
                    count("phases", normalize_phase(item.get("phase")))
                    for value in item.get("countries") or []: count("countries", value)
                # Country facets only count records that carry recruitment-
                # country data (live portal rows have none).  Report the gap
                # so the UI can show "N more have no country data" instead of
                # the buckets reading like a miscount against `total`.
                facets["countries_missing"] = sum(
                    1 for item in items if not (item.get("countries") or []))
                if sort == "start_date":
                    items.sort(key=lambda item: (item.get("startDate") or "", item["id"]), reverse=True)
                elif sort == "id":
                    items.sort(key=lambda item: (item.get("source") or "", item["id"]))
                else:
                    items.sort(key=lambda item: (item.get("lastUpdated") or "", item["id"]), reverse=True)
                total = len(items)
                # Zero-result honesty (#44): when structured filters empty out a
                # query that matches globally, report how many trials the query
                # alone would find, so the UI can say "filters hid them" instead
                # of implying the topic does not exist.
                unfiltered_total = None
                if total == 0 and len(rules) > 1:
                    query_only = {k: v for k, v in rules.items() if k == "query"}
                    unfiltered_rows = matching_trials(conn, query_only)
                    if alt_query:
                        alt_only = matching_trials(
                            conn, {**query_only, "query": alt_query})
                        seen_keys = {
                            (r["short_name"], r["source_trial_id"])
                            for r in unfiltered_rows}
                        unfiltered_rows = list(unfiltered_rows) + [
                            r for r in alt_only
                            if (r["short_name"], r["source_trial_id"]) not in seen_keys]
                    unfiltered_total = len(unfiltered_rows)
                data_as_of = _data_as_of(conn)
                return {"generated_at": _now(), "data_as_of": data_as_of,
                        "rules": rules,
                        "expanded_query": [q_clean, alt_query] if alt_query else None,
                        "segmented_query": segmented,
                        "total": total, "unfiltered_total": unfiltered_total,
                        "sort": sort, "items": items, "facets": facets}

        result = cache.get(rule_key, build)
        start = (page - 1) * page_size
        # Do not expose or mutate the cached list.  Keeping pagination outside
        # the cache also prevents one page size from multiplying large cached
        # result sets under distinct keys.
        return {
            key: value for key, value in result.items() if key != "items"
        } | {
            "page": page,
            "page_size": page_size,
            "trials": result["items"][start:start + page_size],
        }

    @app.get("/api/trials")
    def trials(
        profile: str = Query("mi", description="Disease profile key"),
        keywords: Optional[str] = Query(
            None, description="Comma-separated search keywords; default = profile keywords"),
        source: Optional[str] = Query(None, description="Registry short_name filter (e.g. NCT)"),
        status: Optional[str] = Query(
            None, description="Filter bucket: recruiting/completed/terminated/other"),
        phase: Optional[str] = Query(None, description="Substring match on study phase"),
        country: Optional[str] = Query(None, description="Substring match on countries"),
        sponsor: Optional[str] = Query(None, description="Substring match on sponsors"),
        page: int = Query(1, ge=1),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=0,
                               description="0 = full list (static-JSON parity)"),
    ) -> dict:
        """Trial search — query_trials semantics (FTS prefilter + word-boundary
        re-filter + AMI disambiguation), shaped to the exact Trial contract
        the viewer guards validate."""
        _require_profile(profile)
        meta = DISEASE_PROFILES[profile]

        kw_list = None
        if keywords is not None:
            kw_list = [k.strip() for k in keywords.split(",") if k.strip()] or None
        if kw_list is None:
            # default profile search — the heavy deterministic path, cached
            items = list(cache.get(("trials", profile), lambda: _shaped_trials(profile)))
        else:
            items = [build_light_trial(t) for t in query_trials(keywords=kw_list)]

        if source:
            items = [t for t in items if t["source"] == source]
        if status and status != "all":
            items = [t for t in items if _status_key(t["status"]) == status]
        if phase:
            p = phase.lower()
            items = [t for t in items if p in (t.get("phase") or "").lower()]
        if country:
            c = country.lower()
            items = [t for t in items if any(c in x.lower() for x in t["countries"])]
        if sponsor:
            s = sponsor.lower()
            items = [t for t in items if any(s in x.lower() for x in t["sponsors"])]

        total = len(items)
        if page_size:
            start = (page - 1) * page_size
            items = items[start:start + page_size]

        with closing(_open_conn()) as conn:
            data_as_of = _data_as_of(conn)
        return {
            "generated_at": _now(),
            "data_as_of": data_as_of,
            "profile": profile,
            "label": meta["label"],
            "label_en": meta["label_en"],
            "total": total,
            "trials": items,
        }

    @app.post("/api/trials/{source}/{trial_id}/watch")
    def watch_trial(source: str, trial_id: str) -> dict:
        """Idempotently enable a persisted local watch for an existing trial."""
        with closing(_open_conn()) as conn:
            _require_trial(conn, source, trial_id)
            conn.execute(
                """INSERT INTO trial_watches (trial_id, enabled) VALUES (?, 1)
                   ON CONFLICT(trial_id) DO UPDATE SET enabled=1, updated_at=datetime('now')""",
                (trial_id,),
            )
            watch = conn.execute("SELECT * FROM trial_watches WHERE trial_id=?", (trial_id,)).fetchone()
            for group in WATCH_ALL_GROUPS:
                conn.execute(
                    """INSERT OR IGNORE INTO watch_preferences
                       (trial_watch_id, field_group, enabled, minimum_severity) VALUES (?, ?, ?, 'normal')""",
                    (watch["id"], group, int(group in WATCH_DEFAULTS)),
                )
            conn.commit()
            return {"generated_at": _now(), "watch": _watch_dict(conn, watch)}

    @app.delete("/api/trials/{source}/{trial_id}/watch")
    def unwatch_trial(source: str, trial_id: str) -> dict:
        with closing(_open_conn()) as conn:
            _require_trial(conn, source, trial_id)
            conn.execute("UPDATE trial_watches SET enabled=0, updated_at=datetime('now') WHERE trial_id=?", (trial_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM trial_watches WHERE trial_id=?", (trial_id,)).fetchone()
            return {"generated_at": _now(), "watch": _watch_dict(conn, row) if row else None}

    @app.get("/api/trials/{source}/{trial_id}/watch")
    def trial_watch(source: str, trial_id: str) -> dict:
        with closing(_open_conn()) as conn:
            _require_trial(conn, source, trial_id)
            row = conn.execute("SELECT * FROM trial_watches WHERE trial_id=?", (trial_id,)).fetchone()
            return {"generated_at": _now(), "watch": _watch_dict(conn, row) if row else None}

    @app.get("/api/watches")
    def watched_trials() -> dict:
        """Summaries use correlated aggregates inside one SQL statement, not N+1 queries."""
        with closing(_open_conn()) as conn:
            rows = conn.execute(
                """SELECT w.*, r.title, r.source_trial_id, r.last_updated_at_source,
                          st.label current_status,
                          GROUP_CONCAT(DISTINCT s.short_name) registries,
                          (SELECT MAX(te.detected_at) FROM trial_events te JOIN registry_records er ON er.record_id=te.record_id
                           WHERE er.source_trial_id=w.trial_id) last_changed_at,
                          (SELECT COUNT(*) FROM trial_events te JOIN registry_records er ON er.record_id=te.record_id
                           WHERE er.source_trial_id=w.trial_id AND (w.last_change_seen_at IS NULL OR te.detected_at>w.last_change_seen_at)) unseen_change_count,
                          (SELECT MAX(CASE COALESCE(te.severity, 'normal') WHEN 'critical' THEN 4 WHEN 'important' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END)
                           FROM trial_events te JOIN registry_records er ON er.record_id=te.record_id
                           WHERE er.source_trial_id=w.trial_id AND (w.last_change_seen_at IS NULL OR te.detected_at>w.last_change_seen_at)) unseen_severity_rank
                   FROM trial_watches w JOIN registry_records r ON r.source_trial_id=w.trial_id AND r.is_latest=1
                   JOIN registry_sources s ON s.source_id=r.source_id LEFT JOIN status_types st ON st.status_type_id=r.status_id
                   WHERE w.enabled=1 GROUP BY w.id ORDER BY last_changed_at DESC, w.updated_at DESC"""
            ).fetchall()
            watches = []
            for row in rows:
                d = _watch_dict(conn, row)
                d.update({"title": d.get("title") or d["trial_id"], "current_status": d.get("current_status"),
                          "registries": (d.get("registries") or "").split(",") if d.get("registries") else [],
                          "unseen_change_count": int(d.get("unseen_change_count") or 0),
                          "highest_unseen_severity": next((s for s, n in SEVERITY_RANK.items() if n == d.get("unseen_severity_rank")), None),
                          "watch_enabled": True})
                watches.append(d)
            return {"generated_at": _now(), "total": len(watches), "watches": watches}

    @app.get("/api/monitors")
    def monitors() -> dict:
        with closing(_open_conn()) as conn:
            rows = [dict(r) for r in conn.execute("""SELECT m.*, COUNT(mt.id) current_trials,
              (SELECT COUNT(*) FROM monitor_events e WHERE e.monitor_id=m.id AND e.event_type='trial_entered' AND json_extract(e.metadata,'$.initial_population') IS NOT 1) new_count,
              (SELECT COUNT(*) FROM monitor_events e WHERE e.monitor_id=m.id AND e.event_type='trial_changed') changed_count,
              (SELECT COUNT(*) FROM monitor_events e WHERE e.monitor_id=m.id AND e.event_type='trial_left') left_count,
              (SELECT MAX(started_at) FROM monitor_runs mr WHERE mr.monitor_id=m.id AND mr.completed_at IS NOT NULL) last_run_at
              FROM monitors m LEFT JOIN monitor_trials mt ON mt.monitor_id=m.id AND mt.currently_matches=1 GROUP BY m.id ORDER BY m.updated_at DESC""").fetchall()]
            for row in rows:
                row["enabled"] = bool(row["enabled"])
                row["schedule_enabled"] = bool(row["schedule_enabled"])
                row["email_notifications_enabled"] = bool(row["email_notifications_enabled"])
            return {"generated_at": _now(), "monitors": rows}

    @app.get("/api/notifications")
    def notifications(unread: bool = False, monitor_id: Optional[int] = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict:
        with closing(_open_conn()) as conn:
            where=["n.channel='in_app'"]; args: List[Any]=[]
            if unread: where.append("n.read_at IS NULL")
            if monitor_id is not None: where.append("n.monitor_id=?"); args.append(monitor_id)
            clause="WHERE "+" AND ".join(where)
            rows=[dict(r) for r in conn.execute(f"""SELECT n.*,COALESCE(m.name,n.monitor_name_snapshot) monitor_name,
              (SELECT title FROM registry_records rr WHERE rr.source_trial_id=n.trial_id AND rr.is_latest=1 LIMIT 1) trial_title,
              (SELECT src.short_name FROM registry_records rr JOIN registry_sources src ON src.source_id=rr.source_id WHERE rr.source_trial_id=n.trial_id AND rr.is_latest=1 ORDER BY CASE src.short_name WHEN 'NCT' THEN 0 ELSE 1 END LIMIT 1) source,
              me.related_change_event_id trial_event_id, te.field_name,te.old_value,te.new_value,te.severity,
              (SELECT status FROM notifications en WHERE en.monitor_event_id=n.monitor_event_id AND en.channel='email' LIMIT 1) email_status,
              (SELECT id FROM notifications en WHERE en.monitor_event_id=n.monitor_event_id AND en.channel='email' LIMIT 1) email_notification_id
              FROM notifications n LEFT JOIN monitors m ON m.id=n.monitor_id LEFT JOIN monitor_events me ON me.id=n.monitor_event_id
              LEFT JOIN trial_events te ON te.event_id=me.related_change_event_id {clause} ORDER BY n.created_at DESC,n.id DESC LIMIT ? OFFSET ?""",args+[limit,offset])]
            return {"generated_at":_now(),"notifications":rows,"limit":limit,"offset":offset}

    @app.get("/api/notifications/unread-count")
    def notification_unread_count() -> dict:
        with closing(_open_conn()) as conn:return {"count":conn.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]}

    @app.patch("/api/notifications/{notification_id}")
    def update_notification(notification_id:int,payload:Dict[str,Any]=Body(...))->dict:
        if "read" not in payload or not isinstance(payload["read"],bool): raise HTTPException(status_code=422,detail="read boolean required")
        with closing(_open_conn()) as conn:
            cur=conn.execute("UPDATE notifications SET read_at=CASE WHEN ? THEN datetime('now') ELSE NULL END,updated_at=datetime('now') WHERE id=?",(payload["read"],notification_id))
            if not cur.rowcount: raise HTTPException(status_code=404,detail="notification not found")
            conn.commit();return {"generated_at":_now(),"id":notification_id,"read":payload["read"]}

    @app.post("/api/notifications/mark-all-read")
    def notifications_mark_all_read() -> dict:
        with closing(_open_conn()) as conn:
            cur=conn.execute("UPDATE notifications SET read_at=datetime('now'),updated_at=datetime('now') WHERE read_at IS NULL");conn.commit()
            return {"generated_at":_now(),"updated":cur.rowcount}

    @app.get("/api/notifications/{notification_id}/attempts")
    def notification_attempts(notification_id: int) -> dict:
        with closing(_open_conn()) as conn:
            if not conn.execute("SELECT 1 FROM notifications WHERE id=?", (notification_id,)).fetchone(): raise HTTPException(status_code=404, detail="notification not found")
            rows = [dict(r) for r in conn.execute("SELECT id,attempt_number,started_at,finished_at,status,error_type,error_message,provider_name,provider_message_id FROM notification_delivery_attempts WHERE notification_id=? ORDER BY attempt_number", (notification_id,))]
            return {"generated_at": _now(), "attempts": rows}

    @app.post("/api/notifications/{notification_id}/retry-email")
    def retry_notification_email(notification_id: int) -> dict:
        from core.notifications import retry_failed_notification
        with closing(_open_conn()) as conn:
            row = conn.execute("SELECT channel,status FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if not row or row["channel"] != "email": raise HTTPException(status_code=404, detail="email notification not found")
            if row["status"] != "failed": raise HTTPException(status_code=409, detail="email notification is not failed")
            try:
                result = retry_failed_notification(conn, notification_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"generated_at": _now(), **result}

    @app.post("/api/notifications/deliver-email")
    def deliver_email_notifications() -> dict:
        from core.notifications import deliver_pending_email_notifications
        with closing(_open_conn()) as conn:
            return {"generated_at": _now(), **deliver_pending_email_notifications(conn)}

    @app.post("/api/monitors")
    def create_monitor(payload: Dict[str, Any] = Body(...)) -> dict:
        import json
        from core.monitors import validate_rules, run_monitor
        from core.notifications import valid_email
        name, rules = str(payload.get("name") or "").strip(), payload.get("rules") or {}
        if not name or len(name) > 120: raise HTTPException(status_code=422, detail="invalid monitor name")
        if not isinstance(payload.get("description"), (str, type(None))) or len(payload.get("description") or "") > 2000:
            raise HTTPException(status_code=422, detail="invalid monitor description")
        try: validate_rules(rules)
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc
        schedule_enabled = bool(payload.get("schedule_enabled", False)); frequency = payload.get("schedule_frequency") or "daily"
        if schedule_enabled and frequency not in {"hourly", "daily", "weekly"}: raise HTTPException(status_code=422, detail="unsupported schedule frequency")
        if payload.get("schedule_timezone", "UTC") != "UTC": raise HTTPException(status_code=422, detail="only UTC schedules are currently supported")
        email_enabled = bool(payload.get("email_notifications_enabled", False)); recipient = payload.get("email_recipient")
        if email_enabled and not (recipient := valid_email(recipient)): raise HTTPException(status_code=422, detail="a valid email recipient is required when email notifications are enabled")
        with closing(_open_conn()) as conn:
            next_run = None
            if schedule_enabled:
                from core.monitor_scheduler import next_occurrence
                next_run = next_occurrence(datetime.now(timezone.utc), frequency)
            mid = conn.execute("""INSERT INTO monitors (name,description,schedule_enabled,schedule_frequency,schedule_timezone,next_run_at,email_notifications_enabled,email_recipient,email_enabled_at)
              VALUES (?,?,?,?,?,?,?,?,CASE WHEN ? THEN datetime('now') ELSE NULL END)""", (name, payload.get("description"), int(schedule_enabled), frequency if schedule_enabled else None, "UTC", next_run, int(email_enabled), recipient or valid_email(payload.get("email_recipient")), int(email_enabled))).lastrowid
            conn.execute("INSERT INTO monitor_rules (monitor_id,rules_json) VALUES (?,?)", (mid,json.dumps(rules,ensure_ascii=False)))
            summary = run_monitor(conn, mid)
            return {"generated_at":_now(),"id":mid,"summary":summary}

    @app.post("/api/monitors/preview")
    def monitor_preview(payload: Dict[str, Any] = Body(...)) -> dict:
        from core.monitors import validate_rules, matching_trial_ids
        rules=payload.get("rules") or {}
        try: validate_rules(rules)
        except ValueError as exc: raise HTTPException(status_code=422,detail=str(exc)) from exc
        with closing(_open_conn()) as conn:
            ids=matching_trial_ids(conn,rules)
            return {"matched_count":len(ids),"trial_ids":ids[:10]}

    @app.post("/api/monitors/{monitor_id}/run")
    def run_topic_monitor(monitor_id:int)->dict:
        from core.monitors import run_monitor
        with closing(_open_conn()) as conn:
            try: return {"generated_at":_now(),"summary":run_monitor(conn,monitor_id)}
            except ValueError: raise HTTPException(status_code=404,detail="monitor not found")

    @app.get("/api/monitors/{monitor_id}")
    def monitor_detail(monitor_id:int)->dict:
        with closing(_open_conn()) as conn:
            row=conn.execute("SELECT m.*,mr.rules_json FROM monitors m JOIN monitor_rules mr ON mr.monitor_id=m.id WHERE m.id=?",(monitor_id,)).fetchone()
            if not row: raise HTTPException(status_code=404,detail="monitor not found")
            import json
            out=dict(row); out["rules"]=json.loads(out.pop("rules_json")); return {"generated_at":_now(),"monitor":out}

    @app.patch("/api/monitors/{monitor_id}")
    def update_monitor(monitor_id:int,payload:Dict[str,Any]=Body(...))->dict:
        import json
        from core.monitors import validate_rules
        from core.notifications import valid_email
        if "name" in payload and (not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 120):
            raise HTTPException(status_code=422, detail="invalid monitor name")
        if "description" in payload and payload["description"] is not None and (not isinstance(payload["description"], str) or len(payload["description"]) > 2000):
            raise HTTPException(status_code=422, detail="invalid monitor description")
        if "schedule_enabled" in payload and not isinstance(payload["schedule_enabled"], bool):
            raise HTTPException(status_code=422, detail="schedule_enabled must be boolean")
        # Pinned next run: the scheduler compares next_run_at as a plain UTC
        # "YYYY-MM-DD HH:MM[:SS]" string, so anything else stored here would
        # silently break due-slot matching — normalize or reject.
        next_run_override = payload.get("next_run_at")
        if next_run_override is not None:
            if not isinstance(next_run_override, str):
                raise HTTPException(status_code=422, detail="next_run_at must be a UTC timestamp string")
            normalized = next_run_override.strip().replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    next_run_override = datetime.strptime(normalized, fmt).strftime("%Y-%m-%d %H:%M:%S"); break
                except ValueError:
                    pass
            else:
                raise HTTPException(status_code=422, detail="next_run_at must be UTC 'YYYY-MM-DD HH:MM[:SS]'")
        with closing(_open_conn()) as conn:
            current_monitor = conn.execute(
                "SELECT schedule_enabled,schedule_frequency,schedule_timezone,next_run_at "
                "FROM monitors WHERE id=?", (monitor_id,)).fetchone()
            if not current_monitor: raise HTTPException(status_code=404,detail="monitor not found")
            if "rules" in payload:
                try: validate_rules(payload["rules"])
                except ValueError as exc: raise HTTPException(status_code=422,detail=str(exc)) from exc
                conn.execute("UPDATE monitor_rules SET rules_json=?,updated_at=datetime('now') WHERE monitor_id=?",(json.dumps(payload["rules"],ensure_ascii=False),monitor_id))
            schedule_changed = any(key in payload for key in (
                "schedule_enabled", "schedule_frequency", "schedule_timezone", "next_run_at"))
            schedule_enabled = (payload["schedule_enabled"] if "schedule_enabled" in payload
                                else bool(current_monitor["schedule_enabled"]))
            frequency = (payload["schedule_frequency"] if "schedule_frequency" in payload
                         else current_monitor["schedule_frequency"])
            timezone_name=payload.get("schedule_timezone")
            if frequency is not None and frequency not in {"hourly","daily","weekly"}: raise HTTPException(status_code=422,detail="unsupported schedule frequency")
            if timezone_name is not None and timezone_name != "UTC": raise HTTPException(status_code=422,detail="only UTC schedules are currently supported")
            email_enabled = payload.get("email_notifications_enabled")
            recipient = payload.get("email_recipient")
            if email_enabled is not None and not isinstance(email_enabled, bool): raise HTTPException(status_code=422, detail="email_notifications_enabled must be boolean")
            if recipient is not None and not (recipient := valid_email(recipient)): raise HTTPException(status_code=422, detail="invalid email recipient")
            if email_enabled is True and not (recipient or conn.execute("SELECT email_recipient FROM monitors WHERE id=?", (monitor_id,)).fetchone()[0]): raise HTTPException(status_code=422, detail="a valid email recipient is required when email notifications are enabled")
            next_run = current_monitor["next_run_at"]
            if schedule_changed:
                if not schedule_enabled:
                    # A manual monitor must never advertise a stale future run.
                    next_run = None
                elif "next_run_at" in payload and payload["next_run_at"] is not None:
                    next_run = next_run_override
                elif (not current_monitor["schedule_enabled"]
                      or frequency != current_monitor["schedule_frequency"]
                      or not next_run):
                    from core.monitor_scheduler import next_occurrence
                    frequency = frequency or "daily"
                    next_run = next_occurrence(datetime.now(timezone.utc), frequency)
            conn.execute("""UPDATE monitors SET name=COALESCE(?,name),description=COALESCE(?,description),enabled=COALESCE(?,enabled),
              schedule_enabled=?,schedule_frequency=?,schedule_timezone=COALESCE(?,schedule_timezone),
              next_run_at=?,email_notifications_enabled=COALESCE(?,email_notifications_enabled),email_recipient=COALESCE(?,email_recipient),
              email_enabled_at=CASE WHEN ? THEN datetime('now') ELSE email_enabled_at END,updated_at=datetime('now') WHERE id=?""",(payload.get("name"),payload.get("description"),payload.get("enabled"),schedule_enabled,frequency,timezone_name,next_run,email_enabled,recipient,email_enabled is True,monitor_id));conn.commit()
            return monitor_detail(monitor_id)

    @app.delete("/api/monitors/{monitor_id}")
    def delete_monitor(monitor_id:int)->dict:
        with closing(_open_conn()) as conn:
            if not conn.execute("SELECT 1 FROM monitors WHERE id=?",(monitor_id,)).fetchone(): raise HTTPException(status_code=404,detail="monitor not found")
            # Only monitor-owned history is removed; source records/events remain immutable.
            for table in ("monitor_events","monitor_runs","monitor_trials","monitor_rules","monitors"): conn.execute(f"DELETE FROM {table} WHERE {'monitor_id' if table!='monitors' else 'id'}=?",(monitor_id,))
            conn.commit();return {"generated_at":_now(),"deleted":monitor_id}

    @app.get("/api/monitors/{monitor_id}/runs")
    def monitor_runs(monitor_id:int)->dict:
        with closing(_open_conn()) as conn:return {"generated_at":_now(),"runs":[dict(r) for r in conn.execute("SELECT * FROM monitor_runs WHERE monitor_id=? ORDER BY id DESC",(monitor_id,))]}

    @app.get("/api/monitors/{monitor_id}/activity")
    def monitor_activity(monitor_id:int)->dict:
        with closing(_open_conn()) as conn:
            rows=[dict(r) for r in conn.execute("""SELECT me.*, me.related_change_event_id trial_event_id, te.field_name,te.old_value,te.new_value,te.severity,te.change_type,
              (SELECT s.short_name FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id
               WHERE r.source_trial_id=me.trial_id AND r.is_latest=1
               ORDER BY CASE s.short_name WHEN 'NCT' THEN 0 WHEN 'ChiCTR' THEN 1 WHEN 'CTR' THEN 2 WHEN 'CTIS' THEN 3 WHEN 'ISRCTN' THEN 4 WHEN 'EUCTR' THEN 5 WHEN 'ICTRP' THEN 6 ELSE 7 END, r.record_id
               LIMIT 1) source
              FROM monitor_events me LEFT JOIN trial_events te ON te.event_id=me.related_change_event_id
              WHERE me.monitor_id=? ORDER BY me.detected_at DESC,me.id DESC""",(monitor_id,))]
            return {"generated_at":_now(),"activity":rows}

    @app.get("/api/updates")
    def updates(
        scope: str = Query("monitored", pattern="^(monitored|watched|monitor|all|project)$"),
        monitor_id: Optional[int] = Query(None, ge=1),
        project_id: Optional[int] = Query(None, ge=1),
        window: str = Query("7d", pattern="^(24h|7d|30d|all)$"),
        registry: Optional[str] = None,
        severity: Optional[str] = Query(None, pattern="^(critical|important|normal|minor|priority)$"),
        category: Optional[str] = Query(None, max_length=50),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
    ) -> dict:
        """Scoped, deduplicated change intelligence over persisted trial events.

        A trial event is selected once regardless of how many monitors matched
        it.  Monitor names are aggregated as context, not duplicated feed
        rows.  Membership-only activity remains in monitor activity because it
        has no underlying trial-event severity or old/new value.
        """
        if scope == "monitor" and monitor_id is None:
            raise HTTPException(status_code=422, detail="monitor_id is required for monitor scope")
        if scope == "project" and project_id is None:
            raise HTTPException(status_code=422, detail="project_id is required for project scope")
        with closing(_open_conn()) as conn:
            from core.intelligence import FIELD_CATEGORIES
            if scope == "project": _project(conn, project_id)
            if scope == "monitor" and conn.execute("SELECT 1 FROM monitors WHERE id=?", (monitor_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="monitor not found")
            where: List[str] = []
            args: List[Any] = []
            if window != "all":
                delta = {"24h": 1, "7d": 7, "30d": 30}[window]
                where.append("te.detected_at > ?")
                args.append((datetime.now(timezone.utc) - timedelta(days=delta)).strftime("%Y-%m-%d %H:%M:%S"))
            if registry:
                where.append("s.short_name=?")
                args.append(registry)
            if severity:
                if severity == "priority":
                    where.append("COALESCE(te.severity, 'normal') IN ('critical','important')")
                else:
                    where.append("COALESCE(te.severity, 'normal')=?")
                    args.append(severity)
            if category:
                category_case = "CASE te.field_name " + " ".join(
                    f"WHEN '{field}' THEN '{name}'" for field, name in FIELD_CATEGORIES.items()
                ) + " ELSE COALESCE(te.change_category,'other') END"
                where.append(f"{category_case}=?")
                args.append(category)
            if scope == "watched":
                where.append("EXISTS (SELECT 1 FROM trial_watches w WHERE w.trial_id=r.source_trial_id AND w.enabled=1)")
            elif scope == "monitor":
                where.append("EXISTS (SELECT 1 FROM monitor_events me WHERE me.monitor_id=? AND me.related_change_event_id=te.event_id)")
                args.append(monitor_id)
            elif scope == "monitored":
                where.append("""(EXISTS (SELECT 1 FROM monitor_events me JOIN monitors m ON m.id=me.monitor_id
                                  WHERE m.enabled=1 AND (me.related_change_event_id=te.event_id OR me.trial_id=r.source_trial_id))
                               OR EXISTS (SELECT 1 FROM monitor_trials mt JOIN monitors m ON m.id=mt.monitor_id
                                          WHERE m.enabled=1 AND mt.trial_id=r.source_trial_id))""")
            elif scope == "project":
                from core.projects import project_event_clause
                project_clause, project_args = project_event_clause(project_id)
                where.append(project_clause)
                args.extend(project_args)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            base = f"""FROM trial_events te
                JOIN registry_records r ON r.record_id=te.record_id
                JOIN registry_sources s ON s.source_id=r.source_id
                LEFT JOIN status_types st ON st.status_type_id=r.status_id
                {clause}"""
            total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
            trial_count = conn.execute(f"SELECT COUNT(DISTINCT r.source_trial_id) {base}", args).fetchone()[0]
            facet_rows = conn.execute(f"SELECT s.short_name, COUNT(*) n {base} GROUP BY s.short_name", args).fetchall()
            severity_rows = conn.execute(f"SELECT COALESCE(te.severity, 'normal') severity, COUNT(*) n {base} GROUP BY COALESCE(te.severity, 'normal')", args).fetchall()
            # Aggregate the same filtered, deduplicated event set as the feed.
            # The client never needs to download all event rows to draw charts.
            trend_rows = conn.execute(f"""SELECT date(te.detected_at) day,
                COALESCE(te.severity, 'normal') severity, COUNT(*) n {base}
                GROUP BY date(te.detected_at), COALESCE(te.severity, 'normal')
                ORDER BY day""", args).fetchall()
            trend_by_day: Dict[str, Dict[str, Any]] = {}
            for trend_row in trend_rows:
                bucket = trend_by_day.setdefault(trend_row["day"], {"date": trend_row["day"],
                    "critical": 0, "important": 0, "normal": 0, "minor": 0})
                bucket[trend_row["severity"]] = trend_row["n"]
            category_rows = conn.execute(f"""SELECT te.field_name, te.change_category,
                COUNT(*) n {base} GROUP BY te.field_name, te.change_category""", args).fetchall()
            categories: Dict[str, int] = {}
            for category_row in category_rows:
                key = FIELD_CATEGORIES.get(category_row["field_name"], category_row["change_category"] or "other")
                categories[key] = categories.get(key, 0) + category_row["n"]
            rows = [dict(r) for r in conn.execute(f"""SELECT te.event_id, te.field_name, te.old_value, te.new_value,
                te.change_category, te.change_type, COALESCE(te.severity, 'normal') severity,
                te.detected_at, r.source_trial_id trial_id, r.title, s.short_name source,
                EXISTS (SELECT 1 FROM trial_watches w WHERE w.trial_id=r.source_trial_id AND w.enabled=1) watched,
                EXISTS (SELECT 1 FROM project_trials pt WHERE pt.project_id=? AND pt.trial_id=r.source_trial_id AND pt.source=s.short_name) project_trial,
                (SELECT GROUP_CONCAT(name, '\u001f') FROM (
                  SELECT DISTINCT m.name name FROM monitor_events me JOIN monitors m ON m.id=me.monitor_id
                  WHERE me.related_change_event_id=te.event_id AND m.enabled=1 ORDER BY m.name
                )) monitor_names,
                (SELECT GROUP_CONCAT(name, '\u001f') FROM (
                  SELECT DISTINCT m.name name FROM project_monitors pm JOIN monitors m ON m.id=pm.monitor_id
                  JOIN monitor_events me ON me.monitor_id=m.id
                  WHERE pm.project_id=? AND (me.related_change_event_id=te.event_id OR me.trial_id=r.source_trial_id)
                  ORDER BY m.name
                )) project_monitor_names
                {base} ORDER BY te.detected_at DESC, te.event_id DESC LIMIT ? OFFSET ?""", [project_id or -1, project_id or -1] + args + [page_size, (page - 1) * page_size]).fetchall()]
            for row in rows:
                row["field_label"], row["field_label_zh"] = _field_label(row["field_name"])
                row["watched"] = bool(row["watched"])
                row["monitors"] = row.pop("monitor_names").split("\u001f") if row.get("monitor_names") else []
                row["project_trial"] = bool(row["project_trial"])
                row["project_monitors"] = row.pop("project_monitor_names").split("\u001f") if row.get("project_monitor_names") else []
            monitor_name = None
            if scope == "monitor":
                monitor_name = conn.execute("SELECT name FROM monitors WHERE id=?", (monitor_id,)).fetchone()[0]
            return {"generated_at": _now(), "scope": scope, "monitor_id": monitor_id, "project_id": project_id,
                    "monitor_name": monitor_name, "window": window, "registry": registry,
                    "severity": severity, "category": category, "total": total, "trial_count": trial_count,
                    "page": page, "page_size": page_size, "items": rows,
                    "facets": {"registries": {r["short_name"]: r["n"] for r in facet_rows},
                               "severities": {r["severity"]: r["n"] for r in severity_rows}},
                    "analytics": {"trends": list(trend_by_day.values()), "categories": categories}}

    @app.get("/api/monitors/{monitor_id}/trials")
    def monitor_trials(monitor_id:int)->dict:
        with closing(_open_conn()) as conn:
            # Membership is one row per trial, but a trial id can have several
            # is_latest source records (native registry + ICTRP mirror, #39).
            # Join exactly one canonical record — native source first — so the
            # view never fans a single membership out into duplicate rows.
            rows=[dict(r) for r in conn.execute("""SELECT mt.*,r.title,s.short_name source,st.label status,r.sponsors,r.conditions,r.interventions,r.last_updated_at_source
              FROM monitor_trials mt LEFT JOIN registry_records r ON r.record_id=(
                SELECT r2.record_id FROM registry_records r2 JOIN registry_sources s2 ON s2.source_id=r2.source_id
                WHERE r2.source_trial_id=mt.trial_id AND r2.is_latest=1
                ORDER BY CASE s2.short_name WHEN 'NCT' THEN 0 WHEN 'ChiCTR' THEN 1 WHEN 'CTR' THEN 2 WHEN 'CTIS' THEN 3 WHEN 'ISRCTN' THEN 4 WHEN 'EUCTR' THEN 5 WHEN 'ICTRP' THEN 6 ELSE 7 END, r2.record_id
                LIMIT 1)
              LEFT JOIN registry_sources s ON s.source_id=r.source_id LEFT JOIN status_types st ON st.status_type_id=r.status_id
              WHERE mt.monitor_id=? ORDER BY mt.currently_matches DESC,mt.last_matched_at DESC""",(monitor_id,))]
            return {"generated_at":_now(),"trials":rows}

    @app.get("/api/watches/{watch_id}/preferences")
    def watch_preferences(watch_id: int) -> dict:
        with closing(_open_conn()) as conn:
            row = conn.execute("SELECT * FROM trial_watches WHERE id=?", (watch_id,)).fetchone()
            if row is None: raise HTTPException(status_code=404, detail="watch not found")
            return {"generated_at": _now(), "watch": _watch_dict(conn, row)}

    @app.patch("/api/watches/{watch_id}/preferences")
    def update_watch_preferences(watch_id: int, preferences: List[Dict[str, Any]] = Body(...)) -> dict:
        with closing(_open_conn()) as conn:
            row = conn.execute("SELECT * FROM trial_watches WHERE id=?", (watch_id,)).fetchone()
            if row is None: raise HTTPException(status_code=404, detail="watch not found")
            for pref in preferences:
                group, severity = pref.get("field_group"), pref.get("minimum_severity", "normal")
                if group not in WATCH_ALL_GROUPS or severity not in SEVERITY_RANK:
                    raise HTTPException(status_code=422, detail="invalid watch preference")
                conn.execute("""INSERT INTO watch_preferences (trial_watch_id, field_group, enabled, minimum_severity) VALUES (?, ?, ?, ?)
                              ON CONFLICT(trial_watch_id, field_group) DO UPDATE SET enabled=excluded.enabled, minimum_severity=excluded.minimum_severity""",
                             (watch_id, group, int(bool(pref.get("enabled", True))), severity))
            conn.execute("UPDATE trial_watches SET updated_at=datetime('now') WHERE id=?", (watch_id,))
            conn.commit()
            return {"generated_at": _now(), "watch": _watch_dict(conn, row)}

    @app.get("/api/trials/{source}/{trial_id}/changes")
    def trial_changes(source: str, trial_id: str, limit: int = Query(100, ge=1, le=500),
                      offset: int = Query(0, ge=0), severity: Optional[str] = None,
                      field_group: Optional[str] = None) -> dict:
        with closing(_open_conn()) as conn:
            _require_trial(conn, source, trial_id)
            rows, total = _event_rows(conn, trial_id, limit=limit, offset=offset, severity=severity, field_group=field_group)
            conn.execute("UPDATE trial_watches SET last_viewed_at=datetime('now'), last_change_seen_at=datetime('now') WHERE trial_id=? AND enabled=1", (trial_id,))
            conn.commit()
            return {"generated_at": _now(), "total": total, "limit": limit, "offset": offset, "events": rows}

    @app.get("/api/trials/{source}/{trial_id}/timeline")
    def trial_timeline(source: str, trial_id: str) -> dict:
        with closing(_open_conn()) as conn:
            _require_trial(conn, source, trial_id)
            rows, _ = _event_rows(conn, trial_id, limit=5000)
            groups: Dict[int, Dict[str, Any]] = {}
            for event in rows:
                group = groups.setdefault(event["record_id"], {"record_id": event["record_id"], "to_version": event["to_version"], "detected_at": event["detected_at"], "source": event["source"], "events": [], "severity": "minor"})
                group["events"].append(event)
                if SEVERITY_RANK.get(event["severity"], 0) > SEVERITY_RANK.get(group["severity"], 0): group["severity"] = event["severity"]
            conn.execute("UPDATE trial_watches SET last_viewed_at=datetime('now'), last_change_seen_at=datetime('now') WHERE trial_id=? AND enabled=1", (trial_id,))
            conn.commit()
            return {"generated_at": _now(), "timeline": sorted(groups.values(), key=lambda g: g["detected_at"], reverse=True)}

    @app.get("/api/trials/{source}/{trial_id}/versions")
    def trial_versions(source: str, trial_id: str) -> dict:
        with closing(_open_conn()) as conn:
            _require_trial(conn, source, trial_id)
            rows = [dict(r) for r in conn.execute("""SELECT r.record_id id, r.version_number, s.short_name registry, r.last_crawled_at retrieved_at,
                r.last_updated_at_source source_updated_at, r.data_hash hash,
                COUNT(te.event_id) change_count, COALESCE(MAX(CASE te.severity WHEN 'critical' THEN 4 WHEN 'important' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END), 0) severity_rank
                FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id LEFT JOIN trial_events te ON te.record_id=r.record_id
                WHERE s.short_name=? AND r.source_trial_id=? GROUP BY r.record_id ORDER BY r.version_number DESC""", (source, trial_id)).fetchall()]
            for r in rows:
                rank = r.pop("severity_rank")
                r["highest_change_severity"] = next((s for s, n in SEVERITY_RANK.items() if n == rank), None)
            return {"generated_at": _now(), "versions": rows}

    @app.get("/api/trials/{source}/{trial_id}/versions/{from_version}/compare/{to_version}")
    def compare_versions(source: str, trial_id: str, from_version: int, to_version: int) -> dict:
        with closing(_open_conn()) as conn:
            old = conn.execute("SELECT r.* FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id WHERE s.short_name=? AND r.source_trial_id=? AND r.version_number=?", (source, trial_id, from_version)).fetchone()
            new = conn.execute("SELECT r.* FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id WHERE s.short_name=? AND r.source_trial_id=? AND r.version_number=?", (source, trial_id, to_version)).fetchone()
            if old is None or new is None: raise HTTPException(status_code=404, detail="version not found")
            return {"generated_at": _now(), "from_version": from_version, "to_version": to_version, "changes": _compare_records(old, new, conn=conn)}

    @app.get("/api/trials/{source}/{trial_id}")
    def trial_detail(source: str, trial_id: str) -> dict:
        """Latest record version: TrialDetailData fields + the trial's full
        version-chain change timeline (oldest first)."""
        with closing(_open_conn()) as conn:
            row = conn.execute(
                _TRIALS_SELECT
                + " WHERE r.is_latest = 1 AND src.short_name = ? AND r.source_trial_id = ?",
                (source, trial_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404,
                                    detail=f"no latest record for {source}:{trial_id}")
            data_as_of = _data_as_of(conn)
            history = _record_change_history(conn, source, trial_id, row)
            # earliest time the trial entered our library (any registry
            # sharing the trial ID) — the timeline's origin node, so freshly
            # registered trials (no changes yet) still show a history
            first_crawled = conn.execute(
                """SELECT MIN(r.first_crawled_at) FROM registry_records r
                   WHERE r.source_trial_id = ?""",
                (trial_id,),
            ).fetchone()[0]
            # memberships in topic monitors — the detail right rail shows which
            # monitors currently match this trial (empty list = none)
            monitor_memberships = [dict(r) for r in conn.execute(
                """SELECT m.id, m.name FROM monitor_trials mt
                   JOIN monitors m ON m.id = mt.monitor_id
                   WHERE mt.trial_id = ? AND mt.currently_matches = 1 AND m.enabled = 1
                   ORDER BY m.name""",
                (trial_id,)).fetchall()]

        row_dict = dict(row)
        detail = build_detail(row_dict, _detail_extractors())
        return {
            "generated_at": _now(),
            "data_as_of": data_as_of,
            "source": source,
            "id": trial_id,
            # full light Trial shape so a deep link renders the header without
            # first loading a profile list (build_light_trial = list contract)
            "trial": build_light_trial(row_dict),
            **detail,
            "endpointTable": build_endpoint_table(row_dict),
            "changeHistory": history,
            "first_crawled_at": first_crawled,
            "monitors": monitor_memberships,
        }

    @app.get("/api/trials/{source}/{trial_id}/mirrors")
    def trial_mirrors(source: str, trial_id: str) -> dict:
        """Sibling registrations of the same master trial (cross-source mirrors)."""
        with closing(_open_conn()) as conn:
            current = conn.execute(
                """SELECT r.source_trial_id, r.title, r.enrollment, r.start_date, r.completion_date,
                          r.sponsors, r.conditions, r.interventions, r.last_updated_at_source,
                          st.label status, s.short_name, r.source_url
                   FROM registry_records r JOIN registry_sources s ON s.source_id = r.source_id
                   LEFT JOIN status_types st ON st.status_type_id=r.status_id
                   WHERE r.is_latest = 1 AND s.short_name = ? AND r.source_trial_id = ?""",
                (source, trial_id),
            ).fetchone()
        if current is None:
            raise HTTPException(status_code=404,
                                detail=f"no latest record for {source}:{trial_id}")
        siblings = fetch_cross_source_siblings().get(f"{source}:{trial_id}", [])
        # sibling light fields — powers the Sources tab's cross-source
        # difference table (field | this record | sibling record)
        enriched = []
        with closing(_open_conn()) as conn:
            for sib in siblings:
                row = conn.execute(
                    """SELECT r.source_trial_id, r.title, r.enrollment, r.start_date, r.completion_date,
                              r.sponsors, r.conditions, r.interventions, r.last_updated_at_source,
                              st.label status, src.short_name short_name, r.source_url
                       FROM registry_records r
                       JOIN registry_sources src ON src.source_id = r.source_id
                       LEFT JOIN status_types st ON st.status_type_id = r.status_id
                       WHERE r.is_latest = 1 AND src.short_name = ? AND r.source_trial_id = ?""",
                    (sib.get("short_name"), sib.get("source_trial_id")),
                ).fetchone()
                enriched.append(dict(row) if row is not None else sib)
        current_dict = dict(current)
        comparable = ("status", "enrollment", "start_date", "completion_date", "sponsors", "conditions", "interventions")
        differences = []
        for sibling in enriched:
            fields = [{"field_name": key, "current_value": current_dict.get(key), "source_value": sibling.get(key)}
                      for key in comparable if str(current_dict.get(key) or "") != str(sibling.get(key) or "")]
            if fields:
                differences.append({"source": sibling.get("short_name"), "trial_id": sibling.get("source_trial_id"), "fields": fields})
        return {
            "generated_at": _now(),
            "source": source,
            "id": trial_id,
            "total": len(enriched), "current": current_dict, "differences": differences,
            "siblings": enriched,
        }

    @app.get("/api/mirrors")
    def mirrors() -> dict:
        """All cross-source mirror groups — same shape as data/mirrors.json."""
        with closing(_open_conn()) as conn:
            payload = build_mirrors(conn=conn)
            payload["data_as_of"] = _data_as_of(conn)
            payload["generated_at"] = _now()
            return payload

    @app.get("/api/registries/status")
    def registries_status() -> dict:
        """Operational ingestion/freshness state; never exposes source credentials."""
        from core.ingestion import registry_health
        return {"generated_at": _now(), "registries": registry_health()}

    @app.get("/api/intelligence/overview")
    def intelligence_overview(scope: str = "monitored", monitor_id: Optional[int] = None,
                              window: str = "7d", registry: str = "all",
                              start: Optional[str] = None, end: Optional[str] = None,
                              project_id: Optional[int] = None) -> dict:
        from core.intelligence import overview
        # The dashboard's loading gate: this aggregate scans the full event
        # history and takes tens of seconds on the small demo box, so it is
        # memoized per filter set (invalidated on any DB write).  trends and
        # briefing below call this function directly and inherit the cache.
        key = ("intel-overview", scope, monitor_id, window, registry, start, end, project_id)

        def build() -> dict:
            with closing(_open_conn()) as conn:
                return {"generated_at": _now(), **overview(conn, scope=scope, monitor_id=monitor_id,
                    window=window, registry=registry, start=start, end=end, project_id=project_id)}

        try:
            return cache.get(key, build)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/intelligence/trends")
    def intelligence_trends(scope: str = "monitored", monitor_id: Optional[int] = None,
                            window: str = "7d", registry: str = "all",
                            start: Optional[str] = None, end: Optional[str] = None,
                            project_id: Optional[int] = None) -> dict:
        payload = intelligence_overview(scope, monitor_id, window, registry, start, end, project_id)
        return {"generated_at": payload["generated_at"], "filters": {k: payload[k] for k in ("scope", "monitor_id", "project_id", "window", "registry", "start", "end")},
                "buckets": payload["trends"], "freshness": payload["freshness"]}

    @app.get("/api/intelligence/monitors")
    def intelligence_monitors(window: str = "7d", registry: str = "all") -> dict:
        from core.intelligence import monitor_comparison
        try:
            with closing(_open_conn()) as conn:
                return {"generated_at": _now(), "window": window, "registry": registry,
                        "monitors": monitor_comparison(conn, window=window, registry=registry)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/intelligence/briefing")
    def intelligence_briefing(scope: str = "monitored", monitor_id: Optional[int] = None,
                              window: str = "7d", registry: str = "all",
                              start: Optional[str] = None, end: Optional[str] = None,
                              project_id: Optional[int] = None) -> dict:
        from core.intelligence import briefing
        payload = intelligence_overview(scope, monitor_id, window, registry, start, end, project_id)
        return {"generated_at": payload["generated_at"], **briefing(payload)}

    @app.get("/api/registries/{registry}/runs")
    def registry_runs(registry: str, limit: int = Query(25, ge=1, le=200),
                      offset: int = Query(0, ge=0), status: Optional[str] = None) -> dict:
        from core.ingestion import REGISTRY_KEYS
        if registry not in REGISTRY_KEYS:
            raise HTTPException(status_code=404, detail="unknown registry")
        with closing(_open_conn()) as conn:
            source = conn.execute("SELECT source_id FROM registry_sources WHERE short_name=?", (CONFIG.sources[registry].short_name,)).fetchone()
            if source is None:
                raise HTTPException(status_code=404, detail="registry source not configured")
            where, args = ["source_id=?"], [source["source_id"]]
            if status:
                where.append("status=?"); args.append(status)
            clause = " AND ".join(where)
            total = conn.execute(f"SELECT COUNT(*) FROM registry_ingestion_runs WHERE {clause}", args).fetchone()[0]
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM registry_ingestion_runs WHERE {clause} ORDER BY run_id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()]
            return {"generated_at": _now(), "registry": registry, "total": total, "runs": rows}

    @app.get("/api/registry-runs/{run_id}")
    def registry_run_detail(run_id: int) -> dict:
        with closing(_open_conn()) as conn:
            row = conn.execute("""SELECT ir.*, s.short_name, s.full_name FROM registry_ingestion_runs ir
                JOIN registry_sources s ON s.source_id=ir.source_id WHERE ir.run_id=?""", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="ingestion run not found")
            failures = [dict(r) for r in conn.execute("SELECT source_trial_id, stage, error_type, error_message, created_at FROM registry_ingestion_failures WHERE run_id=? ORDER BY failure_id", (run_id,)).fetchall()]
            return {"generated_at": _now(), "run": dict(row), "failures": failures}

    @app.post("/api/registries/{registry}/sync")
    def registry_sync(registry: str) -> dict:
        from core.ingestion import REGISTRY_KEYS, run_registry_ingestion
        if registry not in REGISTRY_KEYS:
            raise HTTPException(status_code=404, detail="unknown registry")
        result = run_registry_ingestion(registry, trigger="manual")
        if result["status"] == "failed":
            return result
        return result

    @app.post("/api/registry-runs/{run_id}/retry")
    def retry_registry_run(run_id: int) -> dict:
        from core.ingestion import run_registry_ingestion
        with closing(_open_conn()) as conn:
            row = conn.execute("""SELECT ir.status, s.short_name FROM registry_ingestion_runs ir
                JOIN registry_sources s ON s.source_id=ir.source_id WHERE ir.run_id=?""", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="ingestion run not found")
            if row["status"] not in {"failed", "interrupted"}:
                raise HTTPException(status_code=409, detail="only failed or interrupted runs can be retried")
            registry = next((key for key, cfg in CONFIG.sources.items() if cfg.short_name == row["short_name"]), None)
        if registry is None:
            raise HTTPException(status_code=404, detail="registry adapter not configured")
        return run_registry_ingestion(registry, trigger="retry", retry_of_run_id=run_id)

    @app.get("/api/events")
    def events(
        profile: Optional[str] = Query(None, description="Restrict to a disease profile"),
        limit: int = Query(
            500, ge=0, le=5000,
            description="Maximum events (0 = all events in the selected window)",
        ),
        days: int = Query(0, ge=0, le=365,
                          description="Window in days (0 = all); applies to changes and new trials"),
    ) -> dict:
        """Global field-level change stream — same shape as data/events.json,
        plus a ``daily`` view: day-grouped, per-trial changes and new
        registrations (the at-a-glance "what changed when" render)."""
        if profile is not None:
            _require_profile(profile)
        # 2-5MB payloads built from full event scans (~8s on the demo box):
        # memoize per (profile, limit, days) — the Updates/Trials pages refetch
        # the same window on every open.
        key = ("events", profile, limit, days)

        def build() -> dict:
            pairs = None
            if profile is not None:
                pairs = {
                    (t["short_name"], t["source_trial_id"])
                    for t in query_trials(keywords=DISEASE_PROFILES[profile]["report_keywords"])
                }
            with closing(_open_conn()) as conn:
                rows = build_events(
                    limit=(0 if pairs is not None or limit == 0 else limit),
                    conn=conn,
                )["events"]
                for e in rows:
                    e["field_label"], e["field_label_zh"] = _field_label(e.get("field_name") or "")
                if pairs is not None:
                    rows = [e for e in rows if (e["source"], e["id"]) in pairs]
                if days:
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
                        .strftime("%Y-%m-%d %H:%M:%S")
                    rows = [e for e in rows if (e.get("detected_at") or "") > cutoff]
                if limit:
                    rows = rows[:limit]
                # new registrations — digest's "new trial" semantics, windowed
                # (collect_new_trials' since=None default is a 24h first-run
                # window, so "all time" needs an explicit ancient cutoff)
                since = "2000-01-01 00:00:00" if not days else (
                    datetime.now(timezone.utc) - timedelta(days=days)
                ).strftime("%Y-%m-%d %H:%M:%S")
                new_rows = collect_new_trials(
                    since=since,
                    limit=(10 ** 9 if limit == 0 else max(1000, limit)),
                    conn=conn,
                )
                if pairs is not None:
                    new_rows = [n for n in new_rows
                                if (n["short_name"], n["source_trial_id"]) in pairs]
                data_as_of = _data_as_of(conn)
            return {
                "generated_at": _now(),
                "data_as_of": data_as_of,
                "total": len(rows),
                "events": rows,
                "daily": _build_daily(rows, new_rows),
            }

        return cache.get(key, build)

    @app.get("/api/stats")
    def stats() -> dict:
        """Per-source record counts, refresh-queue state and data freshness."""
        with closing(_open_conn()) as conn:
            by_source = {
                r["short_name"]: r["records"]
                for r in conn.execute(
                    """SELECT s.short_name, COUNT(*) AS records
                       FROM registry_records r
                       JOIN registry_sources s ON s.source_id = r.source_id
                       WHERE r.is_latest = 1
                       GROUP BY s.short_name ORDER BY s.short_name"""
                ).fetchall()
            }
            queue = refresh_queue_stats(conn=conn)
            data_as_of = _data_as_of(conn)
        return {
            "generated_at": _now(),
            "data_as_of": data_as_of,
            "records_by_source": by_source,
            "refresh_queue": queue,
        }

    # ── dashboard aggregate (Phase 3E): one request for the landing view ──
    DASHBOARD_WINDOWS = {"24h": 1, "7d": 7, "30d": 30}

    @app.get("/api/dashboard")
    def dashboard(window: str = Query("7d"), limit: int = Query(20, ge=1, le=100)) -> dict:
        """Everything the dashboard shows in one deterministic payload.

        Counts are real persisted rows in the selected window — new trials
        (library entry), updated trials (distinct trials with detected
        changes), important changes (existing severity classification) and
        watched-trial updates.  ``updates`` is the newest change stream with
        presentation labels; ``monitors`` reuses the list summaries plus
        last/next run; ``watched_activity`` mirrors the watched feed.
        """
        days = DASHBOARD_WINDOWS.get(window)
        if days is None:
            raise HTTPException(status_code=422,
                                detail="window must be one of 24h, 7d, 30d")
        key = ("dashboard", window, limit)

        def build() -> dict:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            with closing(_open_conn()) as conn:
                summary = {
                    "new_trials": conn.execute(
                        "SELECT COUNT(DISTINCT source_trial_id) FROM registry_records WHERE first_crawled_at >= ?",
                        (cutoff,)).fetchone()[0],
                    "updated_trials": conn.execute(
                        """SELECT COUNT(DISTINCT r.source_trial_id) FROM trial_events te
                           JOIN registry_records r ON r.record_id = te.record_id
                           WHERE te.detected_at >= ?""", (cutoff,)).fetchone()[0],
                    "important_changes": conn.execute(
                        """SELECT COUNT(*) FROM trial_events
                           WHERE detected_at >= ? AND COALESCE(severity,'normal') IN ('critical','important')""",
                        (cutoff,)).fetchone()[0],
                    "watched_updates": conn.execute(
                        """SELECT COUNT(*) FROM trial_events te
                           JOIN registry_records r ON r.record_id = te.record_id
                           WHERE te.detected_at >= ?
                             AND r.source_trial_id IN (SELECT trial_id FROM trial_watches WHERE enabled=1)""",
                        (cutoff,)).fetchone()[0],
                }

                watched_ids = [r[0] for r in conn.execute(
                    "SELECT trial_id FROM trial_watches WHERE enabled=1").fetchall()]

                from ct_report.query import translate_status_event_values
                rows = [dict(r) for r in conn.execute(
                    """SELECT te.event_id, te.field_name, te.old_value, te.new_value,
                              te.change_category, te.change_type, COALESCE(te.severity,'normal') severity,
                              te.importance_score, te.detected_at, s.short_name source,
                              r.source_trial_id id, r.title, r.source_url, r.status_id
                       FROM trial_events te
                       JOIN registry_records r ON r.record_id = te.record_id
                       JOIN registry_sources s ON s.source_id = r.source_id
                       WHERE te.detected_at >= ?
                       ORDER BY te.detected_at DESC, te.event_id DESC LIMIT ?""",
                    (cutoff, limit)).fetchall()]
                rows = translate_status_event_values(rows, conn)
                for row in rows:
                    row["field_group"] = _field_group(row["field_name"])
                    row["field_label"], row["field_label_zh"] = _field_label(row["field_name"])
                    row["watched"] = row["id"] in watched_ids

                monitors = [dict(r) for r in conn.execute(
                    """SELECT m.id, m.name, m.enabled, m.schedule_enabled, m.schedule_frequency,
                              m.next_run_at, m.last_checked_at,
                              COUNT(mt.id) current_trials,
                              (SELECT COUNT(*) FROM monitor_events e WHERE e.monitor_id=m.id AND e.event_type='trial_entered' AND json_extract(e.metadata,'$.initial_population') IS NOT 1) new_count,
                              (SELECT COUNT(*) FROM monitor_events e WHERE e.monitor_id=m.id AND e.event_type='trial_changed') changed_count,
                              (SELECT COUNT(*) FROM monitor_events e WHERE e.monitor_id=m.id AND e.event_type='trial_left') left_count,
                              (SELECT MAX(started_at) FROM monitor_runs mr WHERE mr.monitor_id=m.id AND mr.completed_at IS NOT NULL) last_run_at
                       FROM monitors m LEFT JOIN monitor_trials mt ON mt.monitor_id=m.id AND mt.currently_matches=1
                       GROUP BY m.id ORDER BY m.updated_at DESC""").fetchall()]
                for m in monitors:
                    m["enabled"] = bool(m["enabled"])
                    m["schedule_enabled"] = bool(m["schedule_enabled"])

                watched_activity = []
                if watched_ids:
                    placeholders = ",".join("?" * len(watched_ids))
                    wrows = [dict(r) for r in conn.execute(
                        f"""SELECT w.trial_id, w.last_change_seen_at,
                                   MAX(te.detected_at) last_changed_at,
                                   COUNT(te.event_id) change_count,
                                   MAX(CASE COALESCE(te.severity,'normal') WHEN 'critical' THEN 4 WHEN 'important' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END) severity_rank,
                                   GROUP_CONCAT(DISTINCT s.short_name) registries,
                                   MAX(r.title) title,
                                   (SELECT st.label FROM registry_records r2
                                      LEFT JOIN status_types st ON st.status_type_id = r2.status_id
                                      WHERE r2.source_trial_id = w.trial_id AND r2.is_latest = 1 LIMIT 1) current_status
                            FROM trial_watches w
                            JOIN registry_records r ON r.source_trial_id = w.trial_id
                            JOIN registry_sources s ON s.source_id = r.source_id
                            LEFT JOIN trial_events te ON te.record_id = r.record_id AND te.detected_at >= ?
                            WHERE w.enabled = 1 AND w.trial_id IN ({placeholders})
                            GROUP BY w.trial_id
                            ORDER BY last_changed_at DESC""", [cutoff] + watched_ids).fetchall()]
                    for w in wrows:
                        rank = w.pop("severity_rank") or 0
                        w["highest_severity"] = next(
                            (s for s, n in SEVERITY_RANK.items() if n == rank), None)
                        w["registries"] = (w.get("registries") or "").split(",") if w.get("registries") else []
                        watched_activity.append(w)

                data_as_of = _data_as_of(conn)
            return {
                "generated_at": _now(),
                "data_as_of": data_as_of,
                "window": window,
                "summary": summary,
                "updates": rows,
                "monitors": monitors,
                "watched_activity": watched_activity,
            }

        return cache.get(key, build)

    # NCT live proxy state (Phase 7 P2): per-process TTL cache + outbound
    # rate-limit bookkeeping.  Read-only — no backfill into the library.
    nct_live_cache: Dict[Any, tuple] = {}
    nct_live_state = {"last_outbound": 0.0}

    @app.get("/api/nct/live")
    def nct_live(
        keywords: str = Query(..., min_length=2,
                              description="Free-text search forwarded to ClinicalTrials.gov API v2"),
        max_results: int = Query(10, ge=1, le=20),
    ) -> dict:
        """LIVE supplement: thin, cached, rate-limited proxy to the public
        ClinicalTrials.gov API v2.  Results are marked ``live`` — straight
        from the registry, not the local library (whose freshness is the
        last pipeline run, shown as data_as_of).  NCT only: user clicks
        never reach the WAF-protected Chinese registries (red line)."""
        import time as _time

        key = ("search", keywords.strip().lower(), max_results)
        now = _time.monotonic()
        hit = nct_live_cache.get(key)
        if hit and now - hit[0] < NCT_LIVE_TTL_SEC:
            return {**hit[1], "cached": True}

        since = now - nct_live_state["last_outbound"]
        if since < NCT_LIVE_MIN_INTERVAL_SEC:
            _time.sleep(NCT_LIVE_MIN_INTERVAL_SEC - since)
        nct_live_state["last_outbound"] = _time.monotonic()
        try:
            resp = http_get(
                "https://clinicaltrials.gov/api/v2/studies",
                params={"query.term": keywords.strip(), "pageSize": max_results},
                timeout=15,
            )
            resp.raise_for_status()
            studies = resp.json().get("studies", [])
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"ClinicalTrials.gov request failed: {exc}")

        trials = [_nct_live_trial(s) for s in studies]
        payload = {
            "generated_at": _now(),
            "keywords": keywords.strip(),
            "live": True,
            "total": len(trials),
            "trials": trials,
        }
        if len(nct_live_cache) > 64:
            nct_live_cache.clear()
        nct_live_cache[key] = (_time.monotonic(), payload)
        return {**payload, "cached": False}

    # ── watch keywords (Phase 7 P2-2: user-defined subscriptions) ────────
    # The pipeline OR-joins these into the ClinicalTrials.gov crawl query,
    # so new trials matching a watched term enter the library automatically;
    # the digest surfaces per-keyword hits.  First write endpoints — local,
    # single-user deployment only.
    @app.get("/api/watch")
    def watch_list() -> dict:
        """Registered keywords with live match counts against the library."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        with closing(_open_conn()) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT watch_id, keyword, created_at FROM watch_keywords ORDER BY watch_id DESC"
            ).fetchall()]
        watches = []
        for r in rows:
            matches = query_trials(keywords=[r["keyword"]])
            recent = [t for t in matches if (t.get("first_crawled_at") or "") >= cutoff]
            watches.append({
                **r,
                "total_matches": len(matches),
                "recent_hits": len(recent),
                "recent_matches": [{
                    "id": t["source_trial_id"],
                    "source": t["short_name"],
                    "title": t["title"],
                    "url": t["source_url"],
                    "first_crawled_at": t.get("first_crawled_at"),
                } for t in recent[:5]],
            })
        return {"generated_at": _now(), "watches": watches}

    @app.post("/api/watch", status_code=201)
    def watch_add(keyword: str = Query(..., min_length=2, max_length=100)) -> dict:
        kw = keyword.strip()
        if not kw:
            raise HTTPException(status_code=422, detail="keyword is blank")
        import sqlite3 as _sq

        with closing(_open_conn()) as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO watch_keywords (keyword) VALUES (?)", (kw,))
                conn.commit()
            except _sq.IntegrityError:
                raise HTTPException(status_code=409,
                                    detail=f"keyword already registered: {kw}")
            row = conn.execute(
                "SELECT watch_id, keyword, created_at FROM watch_keywords WHERE watch_id = ?",
                (cur.lastrowid,)).fetchone()
        return {"generated_at": _now(), "watch": dict(row)}

    @app.delete("/api/watch/{watch_id}")
    def watch_delete(watch_id: int) -> dict:
        with closing(_open_conn()) as conn:
            cur = conn.execute("DELETE FROM watch_keywords WHERE watch_id = ?",
                               (watch_id,))
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"no watch {watch_id}")
        return {"generated_at": _now(), "deleted": watch_id}

    @app.get("/api/nct/study/{nct_id}")
    def nct_study(nct_id: str) -> dict:
        """LIVE single-trial check (Phase 7 P2): fetch one study straight
        from ClinicalTrials.gov and diff its key fields against the local
        library record — "does this trial have anything new?" without
        waiting for the next pipeline run.  NCT only (WAF red line)."""
        import time as _time

        nid = nct_id.strip().upper()
        if not re.fullmatch(r"NCT\d{6,}", nid):
            raise HTTPException(status_code=422, detail=f"not an NCT id: {nid}")

        key = ("study", nid)
        now = _time.monotonic()
        hit = nct_live_cache.get(key)
        if hit and now - hit[0] < NCT_LIVE_TTL_SEC:
            return {**hit[1], "cached": True}
        since = now - nct_live_state["last_outbound"]
        if since < NCT_LIVE_MIN_INTERVAL_SEC:
            _time.sleep(NCT_LIVE_MIN_INTERVAL_SEC - since)
        nct_live_state["last_outbound"] = _time.monotonic()
        try:
            resp = http_get(f"https://clinicaltrials.gov/api/v2/studies/{nid}", timeout=15)
            if resp.status_code == 404:
                raise HTTPException(status_code=404,
                                    detail=f"{nid} not found on ClinicalTrials.gov")
            resp.raise_for_status()
            raw = resp.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail=f"ClinicalTrials.gov request failed: {exc}")

        remote = _nct_live_trial(raw)
        with closing(_open_conn()) as conn:
            local_row = conn.execute(
                _TRIALS_SELECT
                + " WHERE r.is_latest = 1 AND src.short_name = 'NCT' AND r.source_trial_id = ?",
                (nid,),
            ).fetchone()

        diff: List[Dict[str, Any]] = []
        if local_row is not None:
            local = build_light_trial(dict(local_row))
            for field, label in (
                ("title", "title"), ("status", "status"), ("phase", "phase"),
                ("enrollment", "enrollment"), ("startDate", "start_date"),
                ("completionDate", "completion_date"), ("primaryEndpoint", "primary_endpoint"),
            ):
                lv, rv = local.get(field), remote.get(field)
                if (lv or "") != (rv or ""):
                    diff.append({"field": field, "local": lv, "remote": rv})

        payload = {
            "generated_at": _now(),
            "nct_id": nid,
            "in_library": local_row is not None,
            "live_trial": remote,
            "diff": diff,
        }
        nct_live_cache[key] = (_time.monotonic(), payload)
        return {**payload, "cached": False}

    dist = PROJECT_ROOT / "web" / "dist"
    if (dist / "index.html").exists():
        # Mounted last so /api/* routes always win.  SPA history fallback:
        # unknown non-API paths (deep links like /monitors, /trials/NCT/x)
        # serve index.html instead of 404, so refreshing a deep link works.
        class SPAStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope):  # type: ignore[override]
                from starlette.exceptions import HTTPException as StarletteHTTPException

                # HTML must revalidate on every navigation: index.html is the
                # pointer to hashed assets, and a heuristically cached shell
                # keeps a browser on a deleted bundle after each deploy.
                async def mark_html(response):
                    if response.headers.get("content-type", "").startswith("text/html"):
                        response.headers["Cache-Control"] = "no-cache"
                    return response

                # never swallow the API: unknown /api/* stays a real 404
                if scope.get("path", "").startswith("/api/"):
                    return await super().get_response(path, scope)
                try:
                    response = await super().get_response(path, scope)
                    # Starlette versions differ here: some raise for a
                    # missing path while others return a 404 Response.  A
                    # browser refresh of /trials (or /monitors) needs the SPA
                    # in either case, not an API-shaped 404 document.
                    if response.status_code == 404:
                        return await mark_html(await super().get_response("index.html", scope))
                    return await mark_html(response)
                except StarletteHTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    return await mark_html(await super().get_response("index.html", scope))

        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="web")
    else:
        logger.warning("web/dist/index.html not found — API-only mode "
                       "(run `cd web && npm run build` to serve the SPA)")

    return app
