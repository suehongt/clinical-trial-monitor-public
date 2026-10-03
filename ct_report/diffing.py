"""ct_report.diffing — Report registry + incremental new/changed diffing."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from ct_report.query import get_conn
import logging

logger = logging.getLogger(__name__)


def ensure_registry():
    """Ensure the report_registry table exists.

    DDL is owned by db/schema.py (single source of truth); this only makes
    sure it has been applied, including the trial_ids_json migration.
    """
    from db.schema import ensure_report_registry

    ensure_report_registry()


def _trial_key(t: Dict[str, Any]) -> str:
    """Stable per-record key used for incremental diffing."""
    return f"{t['short_name']}:{t['source_trial_id']}"


def _record_signature(t: Dict[str, Any]) -> str:
    """Stable content signature over the fields actually displayed in the report.

    Used to detect *changed* (not just newly crawled) records between runs.
    """
    fields = (
        t.get("title"), t.get("scientific_title"), t.get("conditions"),
        t.get("study_phase"), t.get("study_type_label"), t.get("status_label"),
        t.get("enrollment"), t.get("sponsors"), t.get("primary_endpoint"),
        t.get("secondary_endpoints"), t.get("last_updated_at_source"),
        t.get("start_date"), t.get("eligibility_criteria"),
        t.get("interventions"), t.get("locations"), t.get("countries"),
    )
    raw = "||".join("" if v is None else str(v) for v in fields)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _load_reported_map(last_report: Dict[str, Any]) -> Dict[str, str]:
    """Load {trial_key: signature} from a previous registry entry."""
    raw = last_report.get("trial_ids_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {k: s for k, s in data}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def get_last_report(report_key: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.execute(
        "SELECT * FROM report_registry WHERE report_key = ?", (report_key,)
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def save_report_registry(
    report_key: str,
    report_file: str,
    trial_ids: List[str],
    params: Dict[str, Any],
    trial_ids_json: Optional[str] = None,
):
    ids_hash = hashlib.sha256(
        "|".join(sorted(trial_ids)).encode()
    ).hexdigest()
    conn = get_conn()
    old = conn.execute(
        "SELECT report_file FROM report_registry WHERE report_key = ?", (report_key,)
    ).fetchone()
    if old is not None and old["report_file"] != str(report_file):
        logger.warning(
            "Report registry pointer change for %s: %s -> %s",
            report_key, old["report_file"], str(report_file),
        )
    conn.execute(
        """INSERT OR REPLACE INTO report_registry
           (report_key, generated_at, report_file, trial_ids_hash, record_count, params, trial_ids_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            report_key,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            str(report_file),
            ids_hash,
            len(trial_ids),
            json.dumps(params, ensure_ascii=False),
            trial_ids_json,
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Report registry updated: %s (%d trials)", report_key, len(trial_ids))
