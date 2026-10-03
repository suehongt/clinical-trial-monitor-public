"""ct_report.query — disease-scoped trial queries against the SQLite registry database."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from ct_report.constants import SEARCH_KEYWORDS
from ct_report.paths import DB_PATH
import logging

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── SQL prefilter paths ──────────────────────────────────────────────────
# records_fts is an external-content FTS5 table (trigram tokenizer) over
# registry_records(title, conditions), kept in sync by an AFTER INSERT trigger
# (registry_records rows are insert-only versioning).  It turns the legacy
# per-keyword LIKE '%kw%' full scan into one index lookup per query — but it
# has two blind spots the WHERE must compensate for:
#   * the trigram tokenizer cannot match strings shorter than 3 characters,
#     and below 3 chars MATCH silently matches NOTHING (no error), so short
#     keywords (e.g. 2-char CJK like 心梗) must stay on the LIKE path;
#   * scientific_title is not indexed — covered with a plain LIKE per keyword.

_TRIALS_SELECT = """
    SELECT
        r.record_id,
        r.source_trial_id,
        r.title,
        r.scientific_title,
        r.enrollment,
        r.registration_date,
        r.start_date,
        r.primary_completion_date,
        r.completion_date,
        r.last_updated_at_source,
        r.conditions,
        r.interventions,
        r.sponsors,
        r.locations,
        r.countries,
        r.study_phase,
        r.study_design,
        r.eligibility_criteria,
        r.primary_endpoint,
        r.secondary_endpoints,
        r.is_bootstrap,
        r.first_crawled_at,
        r.last_crawled_at,
        r.source_url,
        r.raw_payload,
        src.short_name,
        src.full_name,
        st.label AS status_label,
        sty.label AS study_type_label
    FROM registry_records r
    JOIN registry_sources src ON src.source_id = r.source_id
    LEFT JOIN status_types st ON st.status_type_id = r.status_id
    LEFT JOIN study_types sty ON sty.study_type_id = r.study_type_id
"""


def _trials_sql(where: str, since_clause: str) -> str:
    return f"""
        {_TRIALS_SELECT}
        WHERE r.is_latest = 1
          AND ({where})
          {since_clause}
        ORDER BY src.short_name, r.last_crawled_at DESC
    """


def _fts_quote(keyword: str) -> str:
    """Quote a keyword as an FTS5 phrase, doubling any embedded double quote."""
    return '"' + keyword.replace('"', '""') + '"'


def _like_where(keywords: List[str]) -> Tuple[str, list]:
    """Legacy prefilter (the original implementation): OR-join
    title/conditions/scientific_title LIKE '%kw%' over every keyword.

    Kept in one place: it is both the behavioural baseline and the fallback
    path when FTS is unavailable or stale.
    """
    like_clauses: List[str] = []
    params: list[str] = []
    for kw in keywords:
        like_clauses.append("(r.title LIKE ? OR r.conditions LIKE ? OR r.scientific_title LIKE ?)")
        for _ in range(3):
            params.append(f"%{kw}%")
    return " OR ".join(like_clauses), params


def _fts_where(long_kws: List[str], short_kws: List[str]) -> Tuple[str, list]:
    """Hybrid prefilter: ONE records_fts MATCH expression OR-joining every
    keyword of >= 3 characters as a quoted phrase (plus a scientific_title
    LIKE per long keyword, since records_fts does not index that column),
    and the legacy 3-column LIKE group for every shorter keyword."""
    groups: List[str] = []
    params: list[str] = []

    if long_kws:
        match_expr = " OR ".join(_fts_quote(kw) for kw in long_kws)
        groups.append(
            "(r.record_id IN (SELECT rowid FROM records_fts WHERE records_fts MATCH ?)"
            + "".join(" OR r.scientific_title LIKE ?" for _ in long_kws)
            + ")"
        )
        params.append(match_expr)
        for kw in long_kws:
            params.append(f"%{kw}%")

    for kw in short_kws:
        groups.append("(r.title LIKE ? OR r.conditions LIKE ? OR r.scientific_title LIKE ?)")
        for _ in range(3):
            params.append(f"%{kw}%")

    return " OR ".join(groups), params


def _fts_index_row_count(conn: sqlite3.Connection) -> Optional[int]:
    """Number of documents actually present in the records_fts inverted index.

    ``SELECT count(*) FROM records_fts`` cannot be used for this: on
    external-content FTS5 tables a bare count is answered from the content
    table (registry_records), so it keeps reporting rows even after the
    inverted index has been emptied or never backfilled.  The per-document
    ``%_docsize`` shadow table tracks the real index contents instead.
    Returns None when the shadow table is absent (exotic FTS5 configuration) —
    callers must treat unknown as "not ready" (safe = legacy LIKE path).
    """
    try:
        return conn.execute('SELECT count(*) FROM "records_fts_docsize"').fetchone()[0]
    except sqlite3.OperationalError:
        return None


def _fts_ready(conn: sqlite3.Connection) -> bool:
    """Whether records_fts can be trusted as a query prefilter.

    Ready = the virtual table exists (sqlite_master) AND its inverted index
    holds at least one document OR registry_records itself is empty (an empty
    index is then not stale — there is simply nothing to index).  Anything
    else (missing table, empty/stale index over a non-empty content table,
    exotic build) means the legacy all-LIKE path must be used.
    """
    try:
        exists = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='records_fts'"
        ).fetchone()[0]
        if not exists:
            return False
        if conn.execute("SELECT count(*) FROM registry_records").fetchone()[0] == 0:
            return True
        return bool(_fts_index_row_count(conn))
    except sqlite3.OperationalError:
        return False


def query_trials(
    keywords: Optional[List[str]] = None,
    since: Optional[str] = None,
    use_fts: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """按关键词集查询相关试验，返回归一化的记录列表（关键词默认取当前疾病 profile）。

    use_fts selects the SQL prefilter:
      * None  — auto-detect: records_fts trigram prefilter when the index is
        usable (see _fts_ready), otherwise the legacy all-LIKE path;
      * True  — force the FTS path (testing hook);
      * False — force the legacy LIKE path (testing hook / baseline).
    Downstream behaviour (word-boundary re-filter, AMI disambiguation, dedup,
    ``since`` windowing) is identical on both paths.
    """
    if keywords is None:
        keywords = SEARCH_KEYWORDS

    conn = get_conn()

    # Trigram FTS can only match keywords of >= 3 characters; anything shorter
    # (e.g. 2-char CJK like 心梗) matches nothing via MATCH — silently — and
    # must go through the legacy LIKE group.
    long_kws = [kw for kw in keywords if len(kw) >= 3]
    short_kws = [kw for kw in keywords if len(kw) < 3]

    if not long_kws:
        fts_path = False
        path_reason = "no keyword with >= 3 characters"
    elif use_fts is False:
        fts_path = False
        path_reason = "use_fts=False"
    elif use_fts is True:
        fts_path = True
        path_reason = ""
    else:
        fts_path = _fts_ready(conn)
        path_reason = "" if fts_path else "records_fts missing or stale"

    if fts_path:
        where, params = _fts_where(long_kws, short_kws)
        logger.info(
            "Query prefilter: FTS (trigram) for %d keyword(s) >= 3 chars "
            "(scientific_title LIKE covers the FTS blind spot), legacy LIKE for %d short keyword(s)",
            len(long_kws), len(short_kws),
        )
    else:
        where, params = _like_where(keywords)
        logger.info("Query prefilter: LIKE fallback (%s)", path_reason)

    # Incremental: only records added/updated since last report
    since_clause = ""
    if since:
        since_clause = "AND r.last_crawled_at >= ?"
        params.append(since)

    sql = _trials_sql(where, since_clause)

    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        if not fts_path:
            raise
        # Belt-and-braces: exotic SQLite builds without the trigram tokenizer
        # (or a dropped FTS table under forced use_fts=True) must degrade to
        # correct results, not crash.
        logger.warning(
            "Query prefilter: FTS MATCH failed (%s); retrying once with legacy LIKE WHERE", exc,
        )
        where, params = _like_where(keywords)
        if since:
            params.append(since)
        cur = conn.execute(_trials_sql(where, since_clause), params)

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    # ── Word-boundary re-filter ───────────────────────────────────────
    # The SQL LIKE '%kw%' above is a broad pre-filter and introduces
    # substring false positives for short acronyms (e.g. "AMI" matching
    # "examine", "familial", "ketamine", "tranexamic", "pharmacodynamics").
    # Re-require each ASCII keyword to appear as a standalone word (or
    # underscore-delimited token); CJK keywords stay as safe substrings.
    #
    # Ambiguous acronyms (e.g. "AMI" = Acute Myocardial Infarction, but also
    # Arthrogenic Muscle Inhibition / Auditory Midbrain Implant / Agonist
    # muscle Interface) are ONLY accepted when the same record also contains a
    # cardiac context term, so the unrelated senses are excluded.
    _CARDIAC_CONTEXT = (
        "myocard", "infarct", "coronary", "heart", "cardiac", "ischaem",
        "ischem", "stemi", "nstemi", "angina", "vascular", "artery",
        "thrombo", "stent", "pci", "coronar", "acute coronary",
    )
    _AMBIGUOUS = {"ami"}

    def _kw_matches(text: Optional[str], kw: str) -> bool:
        if not text:
            return False
        t = text.lower()
        k = kw.lower()
        if re.search(r"[一-鿿]", k):  # CJK keyword: safe substring match
            return k in t
        # ASCII keyword: require a word boundary (or underscore) on both sides
        return re.search(r"(?:\b|_)" + re.escape(k) + r"(?:\b|_)", t) is not None

    def _kw_accepted(row: Dict[str, Any], kw: str) -> bool:
        if not (
            _kw_matches(row.get("title"), kw)
            or _kw_matches(row.get("conditions"), kw)
            or _kw_matches(row.get("scientific_title"), kw)
        ):
            return False
        if kw.lower() in _AMBIGUOUS:
            blob = " ".join(
                filter(None, (row.get("title"), row.get("conditions"), row.get("scientific_title")))
            ).lower()
            if not any(c in blob for c in _CARDIAC_CONTEXT):
                return False
        return True

    kw_list = keywords or []
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        if any(_kw_accepted(row, kw) for kw in kw_list):
            filtered.append(row)
    logger.info(
        "Word-boundary re-filter: %d -> %d trials (dropped %d substring/ambiguous false positives)",
        len(rows), len(filtered), len(rows) - len(filtered),
    )
    rows = filtered

    # Deduplicate by source_trial_id (keep latest version)
    seen: set[str] = set()
    deduped: list[Dict[str, Any]] = []
    for row in rows:
        key = f"{row['short_name']}:{row['source_trial_id']}"
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    logger.info("Query returned %d unique trials (%d raw rows)", len(deduped), len(rows))
    return deduped


def fetch_cross_source_siblings() -> Dict[str, List[Dict[str, str]]]:
    """Map each latest record to the sibling records of its master trial.

    Siblings come from record_master_map: either mirrored snapshots
    (ICTRP reuses the source registry's ID) or genuine multi-registry
    registrations (NCT↔ChiCTR/CTIS…).  Report cards use this to annotate
    "same trial, listed under more than one registry" so intentional
    duplicates are distinguishable from independent trials.

    Returns {f"{short_name}:{source_trial_id}": [sibling, …]} where each
    sibling is {short_name, source_trial_id, source_url}.
    """
    conn = get_conn()
    cur = conn.execute(
        """SELECT mm.master_trial_id, r.source_trial_id, src.short_name, r.source_url
           FROM record_master_map mm
           JOIN registry_records r ON r.record_id = mm.record_id
           JOIN registry_sources src ON src.source_id = r.source_id
           WHERE r.is_latest = 1
           ORDER BY src.short_name, r.source_trial_id"""
    )
    by_master: Dict[str, List[tuple]] = {}
    for row in cur:
        by_master.setdefault(row["master_trial_id"], []).append(
            (row["short_name"], row["source_trial_id"], row["source_url"])
        )
    conn.close()

    siblings: Dict[str, List[Dict[str, str]]] = {}
    for members in by_master.values():
        if len(members) < 2:
            continue
        for short, tid, url in members:
            siblings[f"{short}:{tid}"] = [
                {"short_name": s, "source_trial_id": t, "source_url": u or ""}
                for s, t, u in members
                if not (s == short and t == tid)
            ]
    return siblings


def extract_from_payload(
    raw_payload: Optional[str],
    *keys: str,
) -> Dict[str, Optional[str]]:
    """Extract fields from the raw JSON or HTML payload.

    For NCT: raw_payload is JSON
    For ChiCTR/CTR: raw_payload is HTML
    """
    result: Dict[str, Optional[str]] = {k: None for k in keys}

    if not raw_payload:
        return result

    # Try JSON (NCT)
    if raw_payload.strip().startswith("{"):
        try:
            data = json.loads(raw_payload)
            for k in keys:
                val = data.get(k)
                if val is not None:
                    result[k] = str(val)
                    # Handle list fields
                    if isinstance(val, list):
                        result[k] = "; ".join(str(v) for v in val)
                    elif isinstance(val, dict):
                        result[k] = json.dumps(val, ensure_ascii=False)
            return result
        except json.JSONDecodeError:
            pass

    # For HTML payloads (ChiCTR/CTR), extract via regex patterns
    # These are parsed fields embedded in the stored Python dict representation
    for k in keys:
        # Try to find 'field_name': 'value' pattern in the string representation
        patterns = [
            rf"'{re.escape(k)}':\s*'([^']*)'",
            rf'"{re.escape(k)}":\s*"([^"]*)"',
        ]
        for pat in patterns:
            m = re.search(pat, raw_payload)
            if m:
                result[k] = m.group(1).strip()
                break

    return result


def translate_status_event_values(
    events: List[Dict[str, Any]],
    conn: sqlite3.Connection,
) -> List[Dict[str, Any]]:
    """In-place translate status_id FK values in event rows to status labels.

    Shared by the per-record history (get_events_for_record) and the global
    change stream (exporter build_events / server /api/events) so both read
    "Recruiting", never a bare integer FK.  Rows lacking status_id events are
    returned untouched.
    """
    if not any(r.get("field_name") == "status_id" for r in events):
        return events
    labels = {
        str(r["status_type_id"]): r["label"]
        for r in conn.execute("SELECT status_type_id, label FROM status_types").fetchall()
    }
    for r in events:
        if r.get("field_name") == "status_id":
            for key in ("old_value", "new_value"):
                v = r.get(key)
                if v is not None and str(v) in labels:
                    r[key] = labels[str(v)]
    return events


def get_events_for_record(record_id: int) -> List[Dict[str, Any]]:
    """取一条记录当前版本检测时点的字段级变更事件（报告变更块用）。

    trial_events.record_id 指向「检测到变化时新建的版本行」，对 is_latest
    记录即当前版本自身——粒度精确，不会把历史版本的旧事件混进来。
    status_id 事件的整型 FK 值原位翻译为状态名。
    """
    conn = get_conn()
    cur = conn.execute(
        """SELECT field_name, old_value, new_value, change_category, detected_at
           FROM trial_events
           WHERE record_id = ?
           ORDER BY event_id ASC""",
        (record_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    rows = translate_status_event_values(rows, conn)
    conn.close()
    return rows
