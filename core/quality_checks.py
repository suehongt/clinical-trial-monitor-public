"""
Cross-source data quality checks for the clinical trial monitor.

Functions for detecting:
- Field completeness per source
- Duplicate records
- False merges (same master trial but different trials)
- Missed merges (same trial across platforms not linked)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple

from db.connection import get_connection

logger = logging.getLogger(__name__)

TRACKED_FIELDS = [
    "title", "scientific_title", "enrollment", "start_date",
    "primary_completion_date", "completion_date", "conditions",
    "interventions", "countries", "sponsors", "study_phase",
    "study_design", "primary_endpoint",
]

# Field-coverage matrix: display label (as used by the report's
# 字段完整度矩阵) -> registry_records column.
COVERAGE_FIELDS: Dict[str, str] = {
    "Primary Endpoint": "primary_endpoint",
    "Secondary Endpoints": "secondary_endpoints",
    "Eligibility": "eligibility_criteria",
    "Locations": "locations",
    "Sponsors": "sponsors",
    "Study Phase": "study_phase",
}


def field_coverage_by_source() -> Dict[str, Dict[str, Tuple[int, int]]]:
    """Per-source, per-field coverage over the latest record versions.

    Returns ``{source_short_name: {field_label: (covered, total)}}`` where
    ``total`` is the number of ``registry_records`` rows with
    ``is_latest = 1`` for the source and ``covered`` counts rows whose
    field is non-NULL and, after TRIM, not one of the "empty" sentinels
    ('', '-', '[]') — the same values the report renders as N/A.

    Returns {} when the registry tables are missing (e.g. a blank
    database), so callers can fall back to static display data.
    """
    sums = ", ".join(
        f"SUM(CASE WHEN {col} IS NOT NULL "
        f"AND TRIM({col}) NOT IN ('', '-', '[]') THEN 1 ELSE 0 END) AS `{label}`"
        for label, col in COVERAGE_FIELDS.items()
    )
    conn = get_connection()
    try:
        cur = conn.execute(
            f"""SELECT s.short_name AS source_name,
                       count(*) AS total,
                       {sums}
                FROM registry_records r
                JOIN registry_sources s ON s.source_id = r.source_id
                WHERE r.is_latest = 1
                GROUP BY s.short_name"""
        )
    except sqlite3.OperationalError:
        logger.warning("field_coverage_by_source: registry tables unavailable")
        return {}
    results: Dict[str, Dict[str, Tuple[int, int]]] = {}
    for row in cur.fetchall():
        total = row["total"]
        results[row["source_name"]] = {
            label: (row[label], total) for label in COVERAGE_FIELDS
        }
    return results


def field_completeness_stats() -> List[Dict[str, Any]]:
    """Per-source, per-field non-null percentage."""
    conn = get_connection()
    cur = conn.execute(
        """SELECT s.short_name, s.source_id
           FROM registry_sources s
           ORDER BY s.short_name"""
    )
    sources = cur.fetchall()

    results: List[Dict[str, Any]] = []
    for src in sources:
        total = conn.execute(
            "SELECT count(*) FROM registry_records WHERE source_id = ?",
            (src["source_id"],),
        ).fetchone()[0]
        if total == 0:
            continue

        row: Dict[str, Any] = {"source": src["short_name"], "total_records": total}
        for field in TRACKED_FIELDS:
            non_null = conn.execute(
                f"SELECT count(*) FROM registry_records "
                f"WHERE source_id = ? AND {field} IS NOT NULL",
                (src["source_id"],),
            ).fetchone()[0]
            pct = round(non_null / total * 100, 1)
            row[f"{field}_pct"] = pct
        results.append(row)
    return results


def detect_duplicates() -> List[Dict[str, Any]]:
    """Detect records with same source + registry_id (duplicate rows)."""
    conn = get_connection()
    cur = conn.execute(
        """SELECT source_id, source_trial_id, count(*) as cnt,
                  group_concat(record_id) as record_ids
           FROM registry_records
           GROUP BY source_id, source_trial_id
           HAVING cnt > 1
           ORDER BY cnt DESC"""
    )
    results = []
    for row in cur.fetchall():
        src = conn.execute(
            "SELECT short_name FROM registry_sources WHERE source_id = ?",
            (row["source_id"],),
        ).fetchone()
        results.append({
            "source": src["short_name"] if src else "?",
            "source_trial_id": row["source_trial_id"],
            "duplicate_count": row["cnt"],
            "record_ids": row["record_ids"],
        })
    return results


def registry_id_multi_master_check() -> List[Dict[str, Any]]:
    """R4: Check if the same registry ID links to different Master Trials.

    e.g. NCT123456 recorded via WHO ICTRP links to Master A, but the same
    NCT123456 from ClinicalTrials.gov links to Master B — that's a problem.
    """
    conn = get_connection()
    cur = conn.execute(
        """SELECT ti.identifier_type, ti.identifier_value,
                  count(DISTINCT rmm.master_trial_id) as master_count,
                  group_concat(DISTINCT rmm.master_trial_id) as master_ids,
                  group_concat(DISTINCT r.record_id) as record_ids
           FROM trial_identifiers ti
           JOIN registry_records r ON r.record_id = ti.record_id
           JOIN record_master_map rmm ON rmm.record_id = r.record_id
           GROUP BY ti.identifier_type, ti.identifier_value
           HAVING master_count > 1
           ORDER BY master_count DESC"""
    )
    results = []
    for row in cur.fetchall():
        results.append({
            "identifier_type": row["identifier_type"],
            "identifier_value": row["identifier_value"],
            "master_count": row["master_count"],
            "master_ids": row["master_ids"],
            "record_ids": row["record_ids"],
        })
    return results


def record_multi_master_check() -> List[Dict[str, Any]]:
    """Find records linked to more than one master trial."""
    conn = get_connection()
    cur = conn.execute(
        """SELECT rmm.record_id, r.source_trial_id,
                  count(*) as master_count,
                  group_concat(rmm.master_trial_id) as master_ids
           FROM record_master_map rmm
           JOIN registry_records r ON r.record_id = rmm.record_id
           GROUP BY rmm.record_id
           HAVING master_count > 1
           ORDER BY master_count DESC"""
    )
    results = []
    for row in cur.fetchall():
        src = conn.execute(
            """SELECT s.short_name FROM registry_records r
               JOIN registry_sources s ON s.source_id = r.source_id
               WHERE r.record_id = ?""",
            (row["record_id"],),
        ).fetchone()
        results.append({
            "record_id": row["record_id"],
            "source_trial_id": row["source_trial_id"],
            "source": src["short_name"] if src else "?",
            "master_count": row["master_count"],
            "master_ids": row["master_ids"],
        })
    return results


def _load_sponsors(rec: Any) -> list[str]:
    """Parse sponsor JSON from a record row and return list of sponsor names.

    Handles two formats:
    - NCT-style: ``[{"name": "...", "role": "lead"}]``
    - ICTRP-style: ``["Sponsor Name"]``
    """
    if not rec["sponsors"]:
        return []
    try:
        sp = json.loads(rec["sponsors"])
        if isinstance(sp, list):
            names = []
            for s in sp:
                if isinstance(s, dict):
                    names.append(s.get("name", ""))
                elif isinstance(s, str):
                    names.append(s)
            return [n for n in names if n]
        return [str(sp)]
    except (json.JSONDecodeError, TypeError):
        return [str(rec["sponsors"])]


def _load_interventions(rec: Any) -> list[str]:
    """Parse interventions JSON from a record row."""
    if not rec["interventions"]:
        return []
    try:
        iv = json.loads(rec["interventions"])
        if isinstance(iv, list):
            return [str(i) for i in iv]
        return [str(iv)]
    except (json.JSONDecodeError, TypeError):
        return [str(rec["interventions"])]


def detect_false_merges() -> List[Dict[str, Any]]:
    """Find suspicious AUTO_CONFIRMED merges that might be false positives.

    Checks:
    1. Different interventions across sources (likely different drugs)
    2. Same sponsor, different protocol numbers (different trials from same org)
    3. Different phases across sources
    4. Master protocol / sub-study pattern
    5. Same disease but different phase suggesting distinct trials
    """
    conn = get_connection()
    suspects: List[Dict[str, Any]] = []

    # Get all master trials with >1 record
    cur = conn.execute(
        """SELECT rmm.master_trial_id, count(*) as record_count
           FROM record_master_map rmm
           GROUP BY rmm.master_trial_id
           HAVING record_count > 1
           ORDER BY record_count DESC
           LIMIT 200"""
    )
    multi_record_masters = cur.fetchall()

    for row in multi_record_masters:
        mtid = row["master_trial_id"]

        # Get all records for this master trial
        records = conn.execute(
            """SELECT r.record_id, r.source_trial_id, r.title,
                      r.interventions, r.study_phase, r.sponsors,
                      r.conditions, r.source_id,
                      s.short_name as source_name
               FROM registry_records r
               JOIN record_master_map rmm ON rmm.record_id = r.record_id
               JOIN registry_sources s ON s.source_id = r.source_id
               WHERE rmm.master_trial_id = ? AND r.is_latest = 1""",
            (mtid,),
        ).fetchall()

        if len(records) < 2:
            continue

        # 1. Different interventions (suggest different drugs)
        interventions_set: set[str] = set()
        for rec in records:
            iv_list = _load_interventions(rec)
            if iv_list:
                interventions_set.add(str(sorted(iv_list)))

        if len(interventions_set) > 1:
            names = [
                f"{r['source_name']}:{r['source_trial_id']}(iv={r['interventions'][:60] if r['interventions'] else 'N/A'})"
                for r in records
            ]
            suspects.append({
                "master_trial_id": mtid,
                "issue": "different_interventions",
                "detail": "Different interventions across sources — possible different drugs",
                "records": "; ".join(names),
                "severity": "high",
            })
            continue  # skip other checks — this is already flagged

        # 2. Same sponsor, different protocol numbers
        sponsors_by_record: list[tuple[str, str, str]] = []  # (source, trial_id, sponsor_name)
        for rec in records:
            sp_list = _load_sponsors(rec)
            lead = sp_list[0] if sp_list else ""
            sponsors_by_record.append((rec["source_name"], rec["source_trial_id"], lead))

        if len(set(s for _, _, s in sponsors_by_record if s)) == 1:
            # Same lead sponsor, check for different protocols (different source_trial_id prefixes)
            prefixes = set()
            for sn, tid, _ in sponsors_by_record:
                prefix = tid.split("-")[0].rstrip("0123456789")
                prefixes.add(prefix)
            if len(prefixes) > 1:
                suspects.append({
                    "master_trial_id": mtid,
                    "issue": "same_sponsor_diff_protocol",
                    "detail": "Same sponsor but different protocol number prefixes",
                    "records": "; ".join(f"{sn}:{tid}" for sn, tid, _ in sponsors_by_record),
                    "severity": "medium",
                })
                continue

        # 3. Different phases
        phases_set = set()
        for rec in records:
            if rec["study_phase"] and rec["study_phase"] != "N/A":
                phases_set.add(rec["study_phase"])
        if len(phases_set) > 1:
            names = [
                f"{r['source_name']}:{r['source_trial_id']}(phase={r['study_phase']})"
                for r in records
            ]
            suspects.append({
                "master_trial_id": mtid,
                "issue": "different_phases",
                "detail": f"Different phases: {phases_set}",
                "records": "; ".join(names),
                "severity": "medium",
            })

        # 4. Sub-study pattern
        titles = [rec["title"] for rec in records if rec["title"]]
        for t in titles:
            lower = t.lower()
            if any(kw in lower for kw in ("sub-study", "substudy", "sub study",
                                           "subset", "ancillary", "extension",
                                           "follow-up", "long-term")):
                suspects.append({
                    "master_trial_id": mtid,
                    "issue": "substudy_in_master",
                    "detail": f"Sub-study/extension title: {t[:120]}",
                    "records": "; ".join(f"{r['source_name']}:{r['source_trial_id']}" for r in records),
                    "severity": "medium",
                })
                break

    return suspects


def detect_missed_merges() -> List[Dict[str, Any]]:
    """Find records that should probably share a master trial but don't.

    Checks:
    1. Unlinked records with high title similarity (cross-platform duplicates)
    2. Chinese / English title pairs from ChiCTR (same ChiCTR ID, diff titles)
    3. Same protocol number linked to different master trials
    4. Enrollment-by-country totals that match another trial's total
    """
    conn = get_connection()
    candidates: List[Dict[str, Any]] = []

    # ── 1. Unlinked records with high title similarity ────────────────
    unlinked = conn.execute(
        """SELECT r.record_id, r.source_trial_id, r.title,
                  r.source_id, s.short_name as source_name
           FROM registry_records r
           JOIN registry_sources s ON s.source_id = r.source_id
           WHERE r.is_latest = 1
             AND r.record_id NOT IN (SELECT record_id FROM record_master_map)
             AND r.title IS NOT NULL
           LIMIT 200"""
    ).fetchall()

    for rec in unlinked:
        title = rec["title"].lower()
        matches = conn.execute(
            """SELECT r2.source_trial_id, r2.title, r2.source_id,
                      s2.short_name as src2_name,
                      rmm.master_trial_id
               FROM registry_records r2
               JOIN registry_sources s2 ON s2.source_id = r2.source_id
               JOIN record_master_map rmm ON rmm.record_id = r2.record_id
               WHERE r2.is_latest = 1
                 AND r2.record_id != ?
                 AND r2.title IS NOT NULL
               LIMIT 50""",
            (rec["record_id"],),
        ).fetchall()

        for m in matches:
            sim = SequenceMatcher(None, title, (m["title"] or "").lower()).ratio()
            if sim >= 0.92:
                candidates.append({
                    "record_id": rec["record_id"],
                    "source_trial_id": rec["source_trial_id"],
                    "source": rec["source_name"],
                    "matched_trial_id": m["source_trial_id"],
                    "matched_source": m["src2_name"],
                    "matched_master_id": m["master_trial_id"],
                    "title_similarity": round(sim, 4),
                    "issue": "high_title_similarity",
                    "detail": f"'{rec['title'][:60]}' ~ '{m['title'][:60]}'",
                })

    # ── 2. ChiCTR Chinese/English title pairs ─────────────────────────
    # ChiCTR stores Chinese title + English secondary title; if they ended up
    # as separate records (different source_trial_id), they should be merged.
    chictr_en_pairs = conn.execute(
        """SELECT r1.record_id as rid_a, r1.source_trial_id as tid_a,
                  r1.title as title_a,
                  r2.record_id as rid_b, r2.source_trial_id as tid_b,
                  r2.title as title_b
           FROM registry_records r1
           JOIN registry_records r2 ON
               r1.source_id = r2.source_id
               AND r1.record_id < r2.record_id
               AND r1.is_latest = 1 AND r2.is_latest = 1
           JOIN registry_sources s ON s.source_id = r1.source_id
           WHERE s.short_name = 'ChiCTR'
             AND r1.title IS NOT NULL AND r2.title IS NOT NULL
             AND r1.title != r2.title
             -- one looks Chinese, one looks English
             AND (
                 (r1.title GLOB '*[一-龥]*' AND NOT r2.title GLOB '*[一-龥]*')
                 OR (r2.title GLOB '*[一-龥]*' AND NOT r1.title GLOB '*[一-龥]*')
             )
           LIMIT 50"""
    ).fetchall()
    for row in chictr_en_pairs:
        # Check they are NOT already in the same master trial
        map_a = conn.execute(
            "SELECT master_trial_id FROM record_master_map WHERE record_id = ?",
            (row["rid_a"],),
        ).fetchone()
        map_b = conn.execute(
            "SELECT master_trial_id FROM record_master_map WHERE record_id = ?",
            (row["rid_b"],),
        ).fetchone()
        if map_a and map_b and map_a["master_trial_id"] == map_b["master_trial_id"]:
            continue  # already properly linked
        candidates.append({
            "record_id": row["rid_a"],
            "source_trial_id": row["tid_a"],
            "source": "ChiCTR",
            "issue": "chinese_english_title",
            "detail": f"ChiCTR CN/EN pair: '{row['title_a'][:60]}' vs '{row['title_b'][:60]}'",
            "matched_trial_id": row["tid_b"],
        })

    # ── 3. Same protocol number → different master trials ─────────────
    # Find records where the raw_payload contains the same protocol number
    # but they're linked to different masters
    proto_candidates = conn.execute(
        """SELECT ti.identifier_value as protocol,
                  count(DISTINCT rmm.master_trial_id) as master_count,
                  group_concat(DISTINCT rmm.master_trial_id) as master_ids,
                  group_concat(DISTINCT ti.record_id) as record_ids
           FROM trial_identifiers ti
           JOIN record_master_map rmm ON rmm.record_id = ti.record_id
           WHERE ti.identifier_type = 'ProtocolNumber'
           GROUP BY ti.identifier_value
           HAVING master_count > 1
           LIMIT 50"""
    ).fetchall()
    for row in proto_candidates:
        candidates.append({
            "record_id": None,
            "source_trial_id": row["protocol"],
            "source": "cross-source",
            "issue": "same_protocol_diff_master",
            "detail": f"Protocol '{row['protocol']}' linked to {row['master_count']} different master trials",
            "matched_trial_id": row["master_ids"],
        })

    # ── 4. Enrollment discrepancy (enrollment by country vs total) ────
    # Compare linked records: if one source has enrollment ~ sum of countries
    # from another source, they likely describe the same trial at different levels
    cur = conn.execute(
        """SELECT rmm.master_trial_id, count(*) as cnt
           FROM record_master_map rmm
           GROUP BY rmm.master_trial_id
           HAVING cnt >= 2
           LIMIT 100"""
    )
    for row in cur.fetchall():
        mtid = row["master_trial_id"]
        recs = conn.execute(
            """SELECT r.record_id, r.source_trial_id, r.enrollment,
                      r.countries, s.short_name as source_name
               FROM registry_records r
               JOIN record_master_map rmm ON rmm.record_id = r.record_id
               JOIN registry_sources s ON s.source_id = r.source_id
               WHERE rmm.master_trial_id = ? AND r.is_latest = 1""",
            (mtid,),
        ).fetchall()
        if len(recs) >= 2:
            enrollments = [(r["source_name"], r["source_trial_id"], r["enrollment"]) for r in recs]
            # If two records have significantly different enrollments (>20% diff)
            for i in range(len(enrollments)):
                for j in range(i + 1, len(enrollments)):
                    e1 = enrollments[i][2]
                    e2 = enrollments[j][2]
                    if e1 and e2 and e1 > 0 and e2 > 0:
                        ratio = max(e1, e2) / min(e1, e2)
                        if ratio >= 1.5:
                            candidates.append({
                                "record_id": recs[i]["record_id"],
                                "source_trial_id": enrollments[i][1],
                                "source": enrollments[i][0],
                                "issue": "enrollment_discrepancy",
                                "detail": f"Enrollment {e1} vs {e2} ({ratio:.1f}x difference)",
                                "matched_trial_id": enrollments[j][1],
                            })

    return candidates


def get_quality_summary() -> Dict[str, Any]:
    """Aggregate all quality checks into a single summary dict."""
    conn = get_connection()

    # Record counts per source
    cur = conn.execute(
        """SELECT s.short_name, count(*) as cnt
           FROM registry_records r
           JOIN registry_sources s ON s.source_id = r.source_id
           GROUP BY s.short_name
           ORDER BY s.short_name"""
    )
    records_per_source = {row["short_name"]: row["cnt"] for row in cur.fetchall()}

    # Total master trials
    cur = conn.execute("SELECT count(*) FROM master_trials")
    master_trial_count = cur.fetchone()[0]

    # Match status counts
    cur = conn.execute(
        """SELECT match_status, count(*) as cnt
           FROM record_master_map
           GROUP BY match_status"""
    )
    match_status_counts = {row["match_status"]: row["cnt"] for row in cur.fetchall()}

    # WHO dedup
    cur = conn.execute(
        """SELECT count(*) FROM registry_records r
           JOIN registry_sources s ON s.source_id = r.source_id
           WHERE s.short_name = 'ICTRP'"""
    )
    who_records = cur.fetchone()[0]

    # Field missing rate (across all sources)
    cur = conn.execute("SELECT count(*) FROM registry_records")
    total_records = cur.fetchone()[0]
    field_missing: Dict[str, float] = {}
    if total_records:
        for field in TRACKED_FIELDS:
            cur = conn.execute(
                f"SELECT count(*) FROM registry_records WHERE {field} IS NULL"
            )
            missing = cur.fetchone()[0]
            field_missing[field] = round(missing / total_records * 100, 1)

    # Source failure rate
    cur = conn.execute(
        """SELECT s.short_name,
                  count(*) as total_runs,
                  sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failures
           FROM crawl_log cl
           JOIN registry_sources s ON s.source_id = cl.source_id
           GROUP BY s.short_name
           ORDER BY s.short_name"""
    )
    source_failures: Dict[str, Dict[str, Any]] = {}
    for row in cur.fetchall():
        rate = round(row["failures"] / row["total_runs"] * 100, 1) if row["total_runs"] else 0
        source_failures[row["short_name"]] = {
            "total_runs": row["total_runs"],
            "failures": row["failures"],
            "failure_rate_pct": rate,
        }

    # Active issues
    false_merges = detect_false_merges()
    missed_merges = detect_missed_merges()
    duplicates = detect_duplicates()
    multi_master = record_multi_master_check()
    id_multi_master = registry_id_multi_master_check()

    # Remaining NULL FKs
    cur = conn.execute(
        "SELECT count(*) FROM registry_records WHERE status_id IS NULL"
    )
    null_status = cur.fetchone()[0]
    cur = conn.execute(
        "SELECT count(*) FROM registry_records WHERE study_type_id IS NULL"
    )
    null_study_type = cur.fetchone()[0]

    # Unlinked records (missed resolution)
    cur = conn.execute(
        """SELECT count(*) FROM registry_records r
           WHERE r.is_latest = 1
             AND r.record_id NOT IN (SELECT record_id FROM record_master_map)"""
    )
    unlinked_records = cur.fetchone()[0]

    summary = {
        "records_per_source": records_per_source,
        "total_master_trials": master_trial_count,
        "match_status_counts": match_status_counts,
        "unlinked_records": unlinked_records,
        "who_ictrp_records": who_records,
        "field_missing_rate_pct": field_missing,
        "null_status_id": null_status,
        "null_study_type_id": null_study_type,
        "source_failure_rates": source_failures,
        "false_merge_candidates": len(false_merges),
        "false_merge_details": false_merges[:10],
        "missed_merge_candidates": len(missed_merges),
        "missed_merge_details": missed_merges[:10],
        "duplicate_records": len(duplicates),
        "duplicate_details": duplicates[:10],
        "multi_master_records": len(multi_master),
        "multi_master_details": multi_master[:10],
        "registry_id_multi_master": len(id_multi_master),
        "registry_id_multi_master_details": id_multi_master[:10],
    }
    # §2.6 L5：发现层漏斗精简版——只新增键，不改任何已有键（向后兼容）
    funnel = discovery_funnel_stats()
    summary["discovery"] = {
        "totals": funnel["totals"],
        "conversion_rate": funnel["conversion_rate"],
        "enriched_per_source": {
            source: states.get("enriched", 0)
            for source, states in funnel["by_source_state"].items()
        },
    }
    return summary


def payload_storage_stats() -> Dict[str, Any]:
    """How much of the registry_records table is raw_payload bytes.

    raw_payload stores the full source response (API JSON or whole HTML
    page) per version row.  This measures its share of the table so the
    "compress raw_payload" decision can be made on data, not guesses.
    """
    conn = get_connection()
    cur = conn.execute(
        """SELECT
               count(*)                                            AS rows_total,
               coalesce(sum(length(raw_payload)), 0)               AS payload_bytes,
               coalesce(sum(length(source_trial_id)) +
                        sum(length(coalesce(title, ''))) +
                        sum(length(coalesce(conditions, ''))) +
                        sum(length(coalesce(eligibility_criteria, ''))), 0)
                                                                   AS sample_other_bytes
           FROM registry_records"""
    )
    row = cur.fetchone()
    payload_bytes = row["payload_bytes"] or 0
    per_source: Dict[str, Dict[str, int]] = {}
    for r in conn.execute(
        """SELECT s.short_name AS src,
                  count(*) AS n,
                  coalesce(sum(length(raw_payload)), 0) AS payload_bytes
           FROM registry_records r
           JOIN registry_sources s ON s.source_id = r.source_id
           GROUP BY 1 ORDER BY 1"""
    ):
        per_source[r["src"]] = {"rows": r["n"], "payload_bytes": r["payload_bytes"]}
    return {
        "rows_total": row["rows_total"],
        "raw_payload_bytes": payload_bytes,
        "raw_payload_per_source": per_source,
        # heuristic indicator: payload bytes vs a sample of parsed columns
        "payload_vs_parsed_sample_ratio": round(
            payload_bytes / row["sample_other_bytes"], 2
        ) if row["sample_other_bytes"] else None,
    }


# ── L5 解析守护：中文源详情页解析金丝雀（§2.6） ─────────────────────────────
# 方案：the project design notes §2.6。
# 详情页 HTML 必须命中 ≥CANARY_MIN_HITS 个该源的关键中文标签，才视为
# 「可解析」；不满足计为 parse_canary_failed（enrich 阶段跳过不写并告警）。
#
# 标签从各采集器 FIELD_LABEL_MAP 实际依赖的中文标签提取（去掉行尾全角
# 冒号，便于对原始 HTML 做子串匹配）。刻意不收录裸前缀 "ChiCTR"/"CTR"：
# 两者在列表页也大量出现，无法区分详情页与列表页。
CANARY_LABELS: Dict[str, List[str]] = {
    # collectors/chictr.py parse_detail_page：
    # 注册号 / 研究疾病 / 纳入标准（人选标准）/ 测量指标 / 排除标准 / 伦理批件号
    "chictr": [
        "注册号",
        "研究疾病",
        "纳入标准",
        "测量指标",
        "排除标准",
        "伦理委员会批件文号",
    ],
    # collectors/chinadrugtrials.py parse_detail_html：
    # 登记号（CTR号）/ 药物名称 / 试验分期 / 入选标准 / 主要与次要研究终点
    "chinadrugtrials": [
        "登记号",
        "药物名称",
        "试验分期",
        "入选标准",
        "主要终点指标及评价时间",
        "次要终点指标及评价时间",
    ],
}

# 详情页至少要命中的关键标签数
CANARY_MIN_HITS = 3


def parse_canary_ok(source_key: str, html: str) -> bool:
    """金丝雀断言：详情页 HTML 命中该源 ≥CANARY_MIN_HITS 个关键标签才可解析。

    纯函数、零 DB 依赖——采集器在增强（enrich）步骤调用它决定
    跳过/告警（parse_canary_failed）。

    - ``html`` 命中标签数 ≥ CANARY_MIN_HITS → True；
    - 未知 ``source_key`` → False 并 log warning（防止配置漂移静默放行）；
    - 空 HTML → False。
    """
    labels = CANARY_LABELS.get(source_key)
    if labels is None:
        logger.warning("parse_canary_ok: unknown source_key %r", source_key)
        return False
    if not html:
        return False
    hits = sum(1 for label in labels if label in html)
    return hits >= CANARY_MIN_HITS


def discovery_funnel_stats() -> Dict[str, Any]:
    """发现层漏斗：discovery_queue 按 (source, state) 计数 + 总转化率。

    一次 GROUP BY 查询聚合，返回::

        {
            "by_source_state": {"ChiCTR": {"pending": 3, "enriched": 2}, ...},
            "totals": {"pending": .., "enriched": .., "failed": .., "skipped": ..},
            "conversion_rate": enriched / (enriched + failed + skipped),
        }

    转化率即「发现→增强转化率」（§2.6）：分母只含三个终态，pending 不计入；
    分母为 0 时为 None。队列为空返回空结构（不报错）；表不存在（旧库）
    同样返回空结构并 log warning。
    """
    empty: Dict[str, Any] = {
        "by_source_state": {},
        "totals": {},
        "conversion_rate": None,
    }
    conn = get_connection()
    try:
        cur = conn.execute(
            """SELECT s.short_name AS source, d.state AS state, count(*) AS cnt
               FROM discovery_queue d
               JOIN registry_sources s ON s.source_id = d.source_id
               GROUP BY s.short_name, d.state"""
        )
    except sqlite3.OperationalError:
        logger.warning("discovery_funnel_stats: discovery_queue unavailable")
        return empty

    by_source_state: Dict[str, Dict[str, int]] = {}
    totals: Dict[str, int] = {}
    for row in cur.fetchall():
        by_source_state.setdefault(row["source"], {})[row["state"]] = row["cnt"]
        totals[row["state"]] = totals.get(row["state"], 0) + row["cnt"]
    if not totals:
        return empty

    enriched = totals.get("enriched", 0)
    terminal = enriched + totals.get("failed", 0) + totals.get("skipped", 0)
    conversion_rate = round(enriched / terminal, 4) if terminal else None
    return {
        "by_source_state": by_source_state,
        "totals": totals,
        "conversion_rate": conversion_rate,
    }
