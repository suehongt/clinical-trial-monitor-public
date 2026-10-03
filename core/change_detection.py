"""
Change Detection — compare current vs previous version of a registry_record
and emit trial_events with event_hash dedup.

Key features:
  - Order-insensitive comparison for JSON array fields
  - Event hash (SHA-256) prevents duplicate reporting across crawl cycles
  - Format-only changes (JSON key order, whitespace) are filtered out
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional

from config import CONFIG
from db.connection import get_connection

logger = logging.getLogger(__name__)

# Map DB column names to human-readable categories
FIELD_CATEGORY: Dict[str, str] = {
    "status_id": "status",
    "enrollment": "enrollment",
    "primary_endpoint": "endpoint",
    "secondary_endpoints": "endpoint",
    "study_phase": "design",
    "study_design": "design",
    "eligibility_criteria": "design",
    "sponsors": "sponsor",
    "locations": "location",
    "countries": "location",
    "start_date": "date",
    "completion_date": "date",
    "primary_completion_date": "date",
    "registration_date": "date",
    "last_updated_at_source": "date",
    "arm_group_interventions": "design",
    "interventions": "design",
    "conditions": "other",
}


# Human-readable labels for tracked fields (digest push lines, report
# change blocks).  Kept next to FIELD_CATEGORY so event field names have
# one display vocabulary.  ``status_id`` events store integer FK values —
# translate them through status_types before display (see digest).
FIELD_LABELS: Dict[str, Dict[str, str]] = {
    "status_id": {"zh": "状态", "en": "Status"},
    "enrollment": {"zh": "入组人数", "en": "Enrollment"},
    "start_date": {"zh": "开始日期", "en": "Start date"},
    "primary_completion_date": {"zh": "主要完成日期", "en": "Primary completion"},
    "completion_date": {"zh": "完成日期", "en": "Completion date"},
    "study_phase": {"zh": "分期", "en": "Phase"},
    "study_design": {"zh": "设计类型", "en": "Design"},
    "sponsors": {"zh": "申办方", "en": "Sponsor"},
    "conditions": {"zh": "适应症", "en": "Conditions"},
    "countries": {"zh": "国家/地区", "en": "Countries"},
    "primary_endpoint": {"zh": "主要终点", "en": "Primary endpoint"},
    "secondary_endpoints": {"zh": "次要终点", "en": "Secondary endpoints"},
}


# ── Pure helpers (testable without DB) ──────────────────────────────────


def _try_parse_json(val: Any) -> Any | None:
    """Parse a string as JSON, returning None on failure."""
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _sort_json(obj: Any) -> Any:
    """Recursively sort lists for order-insensitive comparison."""
    if isinstance(obj, dict):
        return {k: _sort_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return sorted(
            [_sort_json(item) for item in obj],
            key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False),
        )
    return obj


_SEMANTIC_TEXT_FIELDS = {
    "sponsors", "primary_endpoint", "secondary_endpoints", "interventions",
    "arm_group_interventions", "eligibility_criteria",
}


def _semantic_text(value: str) -> str:
    """Conservative source-format normalisation, not fuzzy matching.

    It removes only whitespace/case and terminal formatting punctuation, so
    ``ABC University Hospital`` and ``abc university hospital.`` cannot make
    a fake sponsor event.  It intentionally does not merge abbreviations or
    near names, which could conceal a genuinely different organisation.
    """
    return re.sub(r"[\s\.,;:]+$", "", " ".join(value.split())).casefold()


def _normalise_semantic_json(value: Any) -> Any:
    if isinstance(value, str):
        return _semantic_text(value)
    if isinstance(value, list):
        return [_normalise_semantic_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalise_semantic_json(v) for k, v in value.items()}
    return value


def compare_values(old: Any, new: Any, field_name: str = "") -> bool:
    """Return True if values are effectively different.

    Handles:
    - None / empty equivalence
    - JSON key-order normalisation
    - Order-insensitive array comparison (when field is in config list)
    - Whitespace-only difference suppression
    """
    if old == new:
        return False
    if not old and not new:
        return False

    order_insensitive = field_name in CONFIG.change.order_insensitive_fields

    # Normalise strings
    old_str = str(old).strip() if old is not None else ""
    new_str = str(new).strip() if new is not None else ""

    # Try JSON comparison
    old_parsed = _try_parse_json(old_str)
    new_parsed = _try_parse_json(new_str)

    if old_parsed is not None and new_parsed is not None:
        if field_name in _SEMANTIC_TEXT_FIELDS:
            old_parsed, new_parsed = (_normalise_semantic_json(old_parsed),
                                      _normalise_semantic_json(new_parsed))
        if order_insensitive and isinstance(old_parsed, list):
            old_parsed = _sort_json(old_parsed)
            new_parsed = _sort_json(new_parsed)

        old_normalised = json.dumps(old_parsed, sort_keys=True, ensure_ascii=False)
        new_normalised = json.dumps(new_parsed, sort_keys=True, ensure_ascii=False)
        return old_normalised != new_normalised

    # A source can serialize a one-item endpoint list as a scalar in an
    # adjacent version.  Treat that representation change as noise only when
    # the one normalized item is otherwise identical.
    if field_name in _SEMANTIC_TEXT_FIELDS:
        old_single = old_parsed[0] if isinstance(old_parsed, list) and len(old_parsed) == 1 else old_str
        new_single = new_parsed[0] if isinstance(new_parsed, list) and len(new_parsed) == 1 else new_str
        if not isinstance(old_single, (list, dict)) and not isinstance(new_single, (list, dict)):
            return _semantic_text(str(old_single)) != _semantic_text(str(new_single))

    # Fallback: plain string comparison (after stripping whitespace)
    return old_str != new_str


def _is_portal_live_stub(rec: Any) -> bool:
    """True when the row came from the WHO portal's thin search grid
    (raw_payload marks it ``_portal_live``) rather than a full snapshot."""
    try:
        raw = rec["raw_payload"]
    except (IndexError, KeyError, TypeError):
        return False
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool((payload.get("who_ictrp_record") or {}).get("_portal_live"))


def _normalize_for_hash(val: Optional[str]) -> str:
    """Normalise a value for event_hash computation (JSON-sorted, trimmed)."""
    if val is None:
        return ""
    try:
        parsed = json.loads(val)
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return val.strip()


def compute_event_hash(
    source_id: int, source_trial_id: str, field_name: str, new_value: Optional[str]
) -> str:
    """SHA-256 fingerprint for dedup.

    Incorporates source_id, source_trial_id, field_name, and the
    JSON-normalised new value so that the same change across different
    trials or fields produces different hashes.
    """
    raw = f"{source_id}|{source_trial_id}|{field_name}|{_normalize_for_hash(new_value)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Change detection ────────────────────────────────────────────────────


def detect_changes(record_id: int, previous_record_id: Optional[int] = None) -> List[Dict]:
    """Compare a record with its immediate predecessor.

    Returns a list of change dicts:
      {field_name, old_value, new_value, category}
    """
    conn = get_connection()
    rec = conn.execute(
        "SELECT * FROM registry_records WHERE record_id = ?", (record_id,)
    ).fetchone()
    if not rec:
        return []

    # No previous version → no change to detect
    if rec["version_number"] <= 1 and previous_record_id is None:
        return []

    if previous_record_id:
        prev = conn.execute(
            "SELECT * FROM registry_records WHERE record_id = ?", (previous_record_id,)
        ).fetchone()
    else:
        prev = conn.execute(
            """SELECT * FROM registry_records
               WHERE source_id = ? AND source_trial_id = ? AND version_number = ?
               LIMIT 1""",
            (rec["source_id"], rec["source_trial_id"], rec["version_number"] - 1),
        ).fetchone()

    if not prev:
        return []

    if _is_portal_live_stub(rec) and not _is_portal_live_stub(prev):
        # A thin portal-live row downgrading a full record must not diff:
        # the grid exposes only title/status/date, so every other tracked
        # field would register as a mass clear (the 2026-09-29 ICTRP
        # incident).  Downgrades emit no events.
        logger.warning(
            "Suppressed change detection for record %s: portal-live stub "
            "downgraded full record %s", record_id, prev["record_id"])
        return []

    changes: List[Dict] = []
    tracked = CONFIG.change.tracked_fields

    for field in tracked:
        old_val = prev[field] if field in prev.keys() else None
        new_val = rec[field] if field in rec.keys() else None

        if compare_values(old_val, new_val, field):
            category = FIELD_CATEGORY.get(field, "other")
            changes.append({
                "field_name": field,
                "old_value": str(old_val) if old_val is not None else None,
                "new_value": str(new_val) if new_val is not None else None,
                "category": category,
            })

    return changes


# ── Persistence with dedup ──────────────────────────────────────────────


def detect_and_save_events(
    record_id: int,
    previous_record_id: int,
    source_id: int,
    source_trial_id: str,
) -> int:
    """Detect changes between two versions and persist to trial_events.

    Uses event_hash + INSERT OR IGNORE for dedup, so the same change
    is never recorded twice across crawl cycles.  Each row is classified
    through the central severity rules (core.severity) at save time.

    Returns the number of distinct changes detected (before dedup).
    """
    from core.severity import classify_change

    changes = detect_changes(record_id, previous_record_id)
    if not changes:
        return 0

    conn = get_connection()
    # status_id events carry integer FK values — resolve labels once so the
    # classifier sees "Recruiting"/"Terminated", not bare ids.
    status_labels = {
        str(r["status_type_id"]): r["label"]
        for r in conn.execute("SELECT status_type_id, label FROM status_types").fetchall()
    }
    saved = 0
    for ch in changes:
        event_hash = compute_event_hash(
            source_id, source_trial_id, ch["field_name"], ch["new_value"],
        )
        old_label = status_labels.get(str(ch["old_value"])) if ch["field_name"] == "status_id" else None
        new_label = status_labels.get(str(ch["new_value"])) if ch["field_name"] == "status_id" else None
        severity = classify_change(
            ch["field_name"], ch["old_value"], ch["new_value"], ch["category"],
            old_status_label=old_label, new_status_label=new_label,
        )
        try:
            cur = conn.execute("""
                INSERT OR IGNORE INTO trial_events
                    (record_id, event_type, field_name, old_value, new_value,
                     change_category, event_hash, severity, change_type,
                     importance_score)
                VALUES (?, 'field_change', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record_id,
                ch["field_name"],
                ch["old_value"],
                ch["new_value"],
                ch["category"],
                event_hash,
                severity.severity,
                severity.change_type,
                f"{severity.importance_score:g}",
            ))
            if cur.rowcount == 1:
                saved += 1
        except Exception as exc:
            logger.warning("Failed to save trial_event for %s.%s: %s",
                           source_trial_id, ch["field_name"], exc)

    conn.commit()
    logger.info("Saved %d trial_events for record %d (detected %d changes, %d new)",
                saved, record_id, len(changes), saved)
    return len(changes)


# ── Query helpers ───────────────────────────────────────────────────────


def get_unacknowledged_changes(
    source_short_name: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """Fetch trial_events (unacknowledged), joining for display context."""
    conn = get_connection()
    query = """
        SELECT te.*, r.source_trial_id, r.title, s.short_name as source_name
        FROM trial_events te
        JOIN registry_records r ON r.record_id = te.record_id
        JOIN registry_sources s ON s.source_id = r.source_id
        WHERE te.acknowledged = 0
    """
    params: List[Any] = []
    if source_short_name:
        query += " AND s.short_name = ?"
        params.append(source_short_name)
    if category:
        query += " AND te.change_category = ?"
        params.append(category)
    query += " ORDER BY te.detected_at DESC LIMIT ?"
    params.append(limit)

    cur = conn.execute(query, params)
    return [dict(row) for row in cur.fetchall()]


def acknowledge_event(event_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE trial_events SET acknowledged = 1, acknowledged_at = datetime('now') WHERE event_id = ?",
        (event_id,),
    )
    conn.commit()
