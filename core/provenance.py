"""
Provenance tracking and conflict detection for cross-source data.

Populates ``trial_provenance`` and ``conflict_log`` tables:

- **trial_provenance**: field-level traceability showing which source
  provided each value and whether it originated from a primary source
  or an AGGREGATOR.
- **conflict_log**: detects when two sources disagree on a field for
  the same master trial (e.g. different enrollment counts).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from db.connection import get_connection, transaction

logger = logging.getLogger(__name__)

# Fields we track provenance for
TRACKED_FIELDS = [
    "title",
    "scientific_title",
    "status_id",
    "study_type_id",
    "enrollment",
    "start_date",
    "primary_completion_date",
    "completion_date",
    "conditions",
    "interventions",
    "countries",
    "sponsors",
    "study_phase",
    "study_design",
    "primary_endpoint",
]


def record_provenance(record_id: int, source_id: int) -> int:
    """Extract tracked field values from a registry_record and store provenance.

    Call this after a new record is inserted or updated.
    Returns number of provenance rows written.
    """
    conn = get_connection()
    rec = conn.execute(
        "SELECT * FROM registry_records WHERE record_id = ?", (record_id,),
    ).fetchone()
    if not rec:
        logger.warning("record_provenance: record %d not found", record_id)
        return 0

    # Determine provenance_source from the source's type
    src = conn.execute(
        "SELECT source_type FROM registry_sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    prov_source = "aggregator" if src and src["source_type"] == "AGGREGATOR" else "direct"

    written = 0
    with transaction():
        for field in TRACKED_FIELDS:
            val = rec[field] if field in rec.keys() else None
            if val is not None:
                # Normalise to string for storage
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)
                else:
                    val = str(val)
            conn.execute(
                """INSERT OR REPLACE INTO trial_provenance
                   (record_id, source_id, field_name, field_value,
                    provenance_source, is_conflict)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (record_id, source_id, field, val, prov_source),
            )
            written += 1
    return written


def detect_conflicts(master_trial_id: str) -> int:
    """Compare field values across sources linked to the same master trial.

    Inserts entries into ``conflict_log`` for fields where sources disagree.
    Returns number of new conflicts detected.
    """
    conn = get_connection()
    # Get all records linked to this master trial
    cur = conn.execute(
        """SELECT r.record_id, r.source_id
           FROM registry_records r
           JOIN record_master_map rmm ON rmm.record_id = r.record_id
           WHERE rmm.master_trial_id = ? AND r.is_latest = 1
           ORDER BY r.source_id""",
        (master_trial_id,),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return 0  # only one source, no conflict possible

    # Group by source_id → record_id
    source_records: dict[int, int] = {}
    for row in rows:
        if row["source_id"] not in source_records:
            source_records[row["source_id"]] = row["record_id"]

    source_ids = list(source_records.keys())
    if len(source_ids) < 2:
        return 0

    conflicts = 0
    # Compare each pair of sources
    for i in range(len(source_ids)):
        for j in range(i + 1, len(source_ids)):
            sid_a = source_ids[i]
            sid_b = source_ids[j]
            rid_a = source_records[sid_a]
            rid_b = source_records[sid_b]

            # Get provenance values for both sources
            prov_a = conn.execute(
                "SELECT field_name, field_value FROM trial_provenance "
                "WHERE record_id = ? AND source_id = ?",
                (rid_a, sid_a),
            ).fetchall()
            prov_b = conn.execute(
                "SELECT field_name, field_value FROM trial_provenance "
                "WHERE record_id = ? AND source_id = ?",
                (rid_b, sid_b),
            ).fetchall()

            prov_a_map: dict[str, str] = {r["field_name"]: r["field_value"] for r in prov_a}
            prov_b_map: dict[str, str] = {r["field_name"]: r["field_value"] for r in prov_b}

            all_fields = set(prov_a_map.keys()) | set(prov_b_map.keys())
            for field in sorted(all_fields):
                va = prov_a_map.get(field)
                vb = prov_b_map.get(field)
                if va is not None and vb is not None and va != vb:
                    conn.execute(
                        """INSERT OR IGNORE INTO conflict_log
                           (master_trial_id, field_name,
                            source_a_id, source_b_id, value_a, value_b,
                            resolution)
                           VALUES (?, ?, ?, ?, ?, ?, 'unresolved')""",
                        (master_trial_id, field, sid_a, sid_b, va, vb),
                    )
                    conflicts += 1

    if conflicts:
        logger.info("detect_conflicts: master %s — %d conflict(s) found",
                    master_trial_id, conflicts)
    return conflicts


def provenance_stats() -> dict[str, Any]:
    """Return summary stats about provenance coverage."""
    conn = get_connection()
    cur = conn.execute("SELECT count(*) FROM trial_provenance")
    total = cur.fetchone()[0]

    cur = conn.execute(
        """SELECT src.short_name, count(*) as cnt
           FROM trial_provenance tp
           JOIN registry_sources src ON src.source_id = tp.source_id
           GROUP BY src.short_name
           ORDER BY cnt DESC"""
    )
    per_source = {row["short_name"]: row["cnt"] for row in cur.fetchall()}

    cur = conn.execute(
        "SELECT count(*) FROM conflict_log WHERE resolution = 'unresolved'"
    )
    unresolved_conflicts = cur.fetchone()[0]

    return {
        "total_provenance_entries": total,
        "per_source": per_source,
        "unresolved_conflicts": unresolved_conflicts,
    }
