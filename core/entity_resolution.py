"""
Entity Resolution — link registry_records to master_trials.

Core principles:
  - Every record is linked to exactly one master trial (via record_master_map).
  - No records are ever deleted, only linked or unlinked.
  - Master trials use stable UUIDs (survive re-imports and cross-platform merges).
  - Only high-confidence matches (identifier-based) auto-link.
  - Title-similarity matches go to the resolution queue for manual review.

Matching priority (highest to lowest):
  1. Direct identifier match  — same source_trial_id across platforms → AUTO_CONFIRMED
  2. Cross-registration match — record mentions another platform's ID      → AUTO_CONFIRMED
  3. Protocol number match    — exact protocol ID match                   → AUTO_CONFIRMED
  4. Title similarity         — fuzzy title match                         → POSSIBLE (queued)

Match statuses:
  - AUTO_CONFIRMED  — automatically linked, no review needed
  - POSSIBLE        — queued for human review, not auto-linked
  - REJECTED        — explicitly rejected by reviewer
"""
from __future__ import annotations

import bisect
import csv
import logging
import re
import sqlite3
import uuid as _uuid
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from config import CONFIG
from db.connection import get_connection, transaction

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────────


def _is_aggregator_source(record_id: int) -> bool:
    """Check if a record's source is an AGGREGATOR (e.g. WHO ICTRP)."""
    conn = get_connection()
    row = conn.execute(
        """SELECT src.source_type
           FROM registry_records r
           JOIN registry_sources src ON src.source_id = r.source_id
           WHERE r.record_id = ?""",
        (record_id,),
    ).fetchone()
    return row is not None and row["source_type"] == "AGGREGATOR"


def resolve_record(record_id: int) -> Dict[str, Any]:
    """Full resolution pipeline for a single record.

    Steps:
      1. Extract and persist all identifiers (trial_identifiers table)
      2. Search for matching master trials
      3. Auto-link (AUTO_CONFIRMED), queue (POSSIBLE), or create new

    AGGREGATOR-aware:
      - AGGREGATOR records that get linked don't create "new" discoveries.
      - If an AGGREGATOR record creates a new master, the action is
        'created_via_aggregator' to distinguish from primary-source discoveries.

    Returns dict with keys: action, master_trial_id, match_status [, queue_id].
    """
    from core.identifier_extraction import extract_and_save_identifiers

    is_aggregator = _is_aggregator_source(record_id)

    # Step 1: Extract identifiers
    identifiers = extract_and_save_identifiers(record_id)

    # Step 2: Find matches
    matches = find_master_matches(record_id, identifiers)

    # Step 3: Decision
    auto_confirmed = [m for m in matches if m["match_status"] == "AUTO_CONFIRMED"]
    possible = [m for m in matches if m["match_status"] == "POSSIBLE"]

    if auto_confirmed:
        top = auto_confirmed[0]
        link_record_to_master(
            record_id, top["master_trial_id"],
            method=top["method"], confidence=top["confidence"],
            match_status="AUTO_CONFIRMED",
        )
        logger.info("Record %d AUTO-CONFIRMED → master %s (%s)",
                    record_id, top["master_trial_id"], top["method"])
        return {
            "action": "linked",
            "master_trial_id": top["master_trial_id"],
            "match_status": "AUTO_CONFIRMED",
            "source_type": "AGGREGATOR" if is_aggregator else "PRIMARY",
        }

    if possible:
        top = possible[0]
        # Build evidence_detail summary
        evidence_parts = []
        for m in possible[:3]:
            evidence_parts.append(
                f"{m['method']} (conf={m['confidence']:.2f})"
            )
        evidence_detail = "; ".join(evidence_parts) if evidence_parts else None

        qid = add_to_queue(
            record_id, top["master_trial_id"],
            method=top["method"], confidence=top["confidence"],
            reasoning=top.get("reasoning", ""),
            evidence_detail=evidence_detail,
        )
        logger.info("Record %d QUEUED → master %s (%.2f, %s)",
                    record_id, top["master_trial_id"], top["confidence"], top["method"])
        return {
            "action": "queued",
            "master_trial_id": top["master_trial_id"],
            "match_status": "POSSIBLE",
            "queue_id": qid,
            "source_type": "AGGREGATOR" if is_aggregator else "PRIMARY",
        }

    # No matches at all → create new master trial
    master_id = create_master_trial(record_id)
    action = "created_via_aggregator" if is_aggregator else "created"
    logger.info("Record %d → new master trial %s (type=%s)",
                record_id, master_id, action)
    return {
        "action": action,
        "master_trial_id": master_id,
        "match_status": "AUTO_CONFIRMED",
        "source_type": "AGGREGATOR" if is_aggregator else "PRIMARY",
    }


def find_master_matches(
    record_id: int,
    identifiers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find potential master_trial matches for a record.

    Priority:
      1. Identifier match (same type+value in another linked record) → AUTO_CONFIRMED
         if identifier confidence >= 0.9; otherwise → POSSIBLE.
      2. Title similarity (fallback, only if no AUTO_CONFIRMED) → POSSIBLE.

    Returns list sorted: AUTO_CONFIRMED first, then by descending confidence.
    """
    conn = get_connection()
    results: List[Dict[str, Any]] = []
    seen_masters: set[str] = set()

    # ── Strategy 1-3: Identifier-based matching ──────────────────────
    for idf in identifiers:
        # Find other records with the same identifier that are already linked
        cur = conn.execute(
            """SELECT DISTINCT rmm.master_trial_id
               FROM trial_identifiers ti2
               JOIN record_master_map rmm ON rmm.record_id = ti2.record_id
               WHERE ti2.identifier_type = ? AND ti2.identifier_value = ?
                 AND ti2.record_id != ?""",
            (idf["identifier_type"], idf["identifier_value"], record_id),
        )
        for row in cur.fetchall():
            mtid = row["master_trial_id"]
            if mtid in seen_masters:
                continue
            seen_masters.add(mtid)

            # Known registry IDs → always AUTO_CONFIRMED.
            # Protocol numbers → AUTO_CONFIRMED only if extraction confidence is high.
            is_registry_id = idf["identifier_type"] in (
                "NCT", "ChiCTR", "CTR", "CTIS", "ISRCTN", "EudraCT",
            )
            is_high_conf = idf["confidence"] >= 0.9
            results.append({
                "master_trial_id": mtid,
                "method": (
                    f"identifier_match ({idf['identifier_type']}: "
                    f"{idf['identifier_value']}, conf={idf['confidence']})"
                ),
                "confidence": idf["confidence"],
                "match_status": "AUTO_CONFIRMED" if (is_registry_id or is_high_conf) else "POSSIBLE",
                "reasoning": (
                    f"Record shares {idf['identifier_type']}='{idf['identifier_value']}' "
                    f"(confidence={idf['confidence']}) with existing linked record"
                ),
            })

    # ── Strategy 4: Title similarity (only if no AUTO_CONFIRMED) ─────
    if not any(r["match_status"] == "AUTO_CONFIRMED" for r in results):
        rec = conn.execute(
            "SELECT title FROM registry_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if rec and rec["title"]:
            for row in _title_candidates(rec["title"], record_id):
                if row["master_trial_id"] in seen_masters:
                    continue
                sim = _similarity(rec["title"], row["title"])
                if sim >= CONFIG.er.min_title_similarity:
                    seen_masters.add(row["master_trial_id"])
                    results.append({
                        "master_trial_id": row["master_trial_id"],
                        "method": "title_similarity",
                        "confidence": round(sim, 4),
                        "match_status": "POSSIBLE",
                        "reasoning": (
                            f"Title similarity {sim:.2%} with record "
                            f"{row['record_id']}"
                        ),
                    })

    # Sort: AUTO_CONFIRMED first, then by descending confidence
    results.sort(key=lambda x: (
        0 if x["match_status"] == "AUTO_CONFIRMED" else 1,
        -x["confidence"],
    ))
    return results


# ── Link management ──────────────────────────────────────────────────────


def link_record_to_master(
    record_id: int,
    master_trial_id: str,
    method: str = "manual",
    confidence: float = 1.0,
    match_status: str = "AUTO_CONFIRMED",
) -> bool:
    """Create a record_master_map entry.  Returns True if new link created."""
    conn = get_connection()
    try:
        with transaction():
            cur = conn.execute(
                """INSERT OR IGNORE INTO record_master_map
                   (record_id, master_trial_id, match_method, match_confidence, match_status)
                   VALUES (?, ?, ?, ?, ?)""",
                (record_id, master_trial_id, method, confidence, match_status),
            )
        return cur.rowcount > 0
    except Exception as exc:
        logger.error("Failed to link record %d → master %s: %s",
                     record_id, master_trial_id, exc)
        return False


def create_master_trial(record_id: int) -> str:
    """Create a new master_trial from a registry_record.  Returns the UUID."""
    conn = get_connection()
    rec = conn.execute(
        "SELECT * FROM registry_records WHERE record_id = ?", (record_id,),
    ).fetchone()
    if not rec:
        raise ValueError(f"Record {record_id} not found")

    master_id = str(_uuid.uuid4())
    with transaction():
        conn.execute(
            """INSERT INTO master_trials
               (master_trial_id, preferred_title, scientific_title,
                study_type_id, status_id, conditions, interventions, enrollment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (master_id, rec["title"], rec["scientific_title"],
             rec["study_type_id"], rec["status_id"],
             rec["conditions"], rec["interventions"], rec["enrollment"]),
        )
        conn.execute(
            """INSERT INTO record_master_map
               (record_id, master_trial_id, match_method, match_confidence, match_status)
               VALUES (?, ?, 'first_record', 1.0, 'AUTO_CONFIRMED')""",
            (record_id, master_id),
        )
    logger.info("Created master_trial %s from record %d", master_id, record_id)
    return master_id


# ── Batch auto-resolution ────────────────────────────────────────────────


def auto_resolve(
    unlinked_record_ids: Optional[List[int]] = None,
) -> Dict[str, int]:
    """Run auto-resolution for records without a master_trial link.

    Returns summary: {linked, created, queued, skipped, total}.
    """
    conn = get_connection()

    if unlinked_record_ids is None:
        cur = conn.execute(
            """SELECT r.record_id FROM registry_records r
               WHERE r.is_latest = 1
                 AND r.record_id NOT IN (SELECT record_id FROM record_master_map)"""
        )
        unlinked_record_ids = [row["record_id"] for row in cur.fetchall()]

    summary: Dict[str, int] = {
        "linked": 0, "created": 0, "created_via_aggregator": 0,
        "queued": 0, "skipped": 0,
        "total": len(unlinked_record_ids),
    }

    for rid in unlinked_record_ids:
        try:
            result = resolve_record(rid)
            action = result["action"]
            if action == "linked":
                summary["linked"] += 1
            elif action == "created":
                summary["created"] += 1
            elif action == "created_via_aggregator":
                summary["created_via_aggregator"] += 1
            elif action == "queued":
                summary["queued"] += 1
            else:
                summary["skipped"] += 1
        except Exception as exc:
            logger.error("Resolution failed for record %d: %s", rid, exc)
            summary["skipped"] += 1

    logger.info("Auto-resolution complete: %d linked, %d created, "
                "%d created via aggregator, %d queued, %d skipped",
                summary["linked"], summary["created"],
                summary["created_via_aggregator"], summary["queued"],
                summary["skipped"])
    return summary


# ── Review Queue ─────────────────────────────────────────────────────────


def add_to_queue(
    record_id: int,
    suggested_master_id: Optional[str],
    method: str,
    confidence: float,
    reasoning: str = "",
    evidence_detail: Optional[str] = None,
) -> int:
    """Add a POSSIBLE match to the resolution queue.  Returns queue_id."""
    conn = get_connection()
    with transaction():
        cur = conn.execute(
            """INSERT INTO resolution_queue
               (record_id, suggested_master_id, match_method, confidence,
                reasoning, evidence_detail, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (record_id, suggested_master_id, method, confidence, reasoning,
             evidence_detail),
        )
        qid = cur.lastrowid
    logger.info("Added queue entry %d for record %d → master %s (%.2f)",
                qid, record_id, suggested_master_id, confidence)
    return qid


def get_review_queue(status: str = "pending") -> List[Dict[str, Any]]:
    """List resolution queue entries with record details."""
    conn = get_connection()
    cur = conn.execute(
        """SELECT q.*, r.source_trial_id, r.title,
                  src.short_name as source_name
           FROM resolution_queue q
           JOIN registry_records r ON r.record_id = q.record_id
           JOIN registry_sources src ON src.source_id = r.source_id
           WHERE q.status = ?
           ORDER BY q.created_at ASC""",
        (status,),
    )
    return [dict(row) for row in cur.fetchall()]


def approve_match(queue_id: int, reviewer: Optional[str] = None) -> bool:
    """Approve a queued match — create the record_master_map entry."""
    conn = get_connection()
    q = conn.execute(
        "SELECT * FROM resolution_queue WHERE queue_id = ?", (queue_id,),
    ).fetchone()
    if not q:
        logger.warning("Queue entry %d not found", queue_id)
        return False
    if q["status"] != "pending":
        logger.warning("Queue entry %d already %s", queue_id, q["status"])
        return False

    with transaction():
        conn.execute(
            """UPDATE resolution_queue
               SET status = 'approved',
                   reviewed_at = datetime('now'),
                   reviewed_by = ?
               WHERE queue_id = ?""",
            (reviewer, queue_id),
        )
        link_record_to_master(
            q["record_id"], q["suggested_master_id"],
            method=q["match_method"], confidence=q["confidence"],
            match_status="AUTO_CONFIRMED",
        )
    logger.info("Approved queue entry %d: record %d → master %s (by %s)",
                queue_id, q["record_id"], q["suggested_master_id"], reviewer)
    return True


def reject_match(queue_id: int, reviewer: Optional[str] = None) -> bool:
    """Reject a queued match — mark as rejected WITHOUT creating a link.

    The record remains unlinked; call ``resolve_record`` again to create
    a new master trial or find another match.
    """
    conn = get_connection()
    q = conn.execute(
        "SELECT * FROM resolution_queue WHERE queue_id = ?", (queue_id,),
    ).fetchone()
    if not q:
        logger.warning("Queue entry %d not found", queue_id)
        return False

    with transaction():
        conn.execute(
            """UPDATE resolution_queue
               SET status = 'rejected',
                   reviewed_at = datetime('now'),
                   reviewed_by = ?
               WHERE queue_id = ?""",
            (reviewer, queue_id),
        )
    logger.info("Rejected queue entry %d: record %d -> master %s rejected (by %s)",
                queue_id, q["record_id"], q["suggested_master_id"], reviewer)
    return True


# ── Helpers ──────────────────────────────────────────────────────────────


def _fts_terms(title: str) -> List[str]:
    """Extract distinctive substrings for trigram FTS matching.

    Trigram matching requires terms of >= 3 characters: ASCII words are used
    whole, CJK runs are sampled as 3-character windows spread across the run.
    """
    terms: List[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", title):
        terms.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", title):
        if len(run) <= 6:
            terms.append(run)
            continue
        windows = len(run) - 2
        step = max(1, windows // 4)
        for i in range(0, windows, step):
            terms.append(run[i:i + 3])
    seen: set[str] = set()
    unique: List[str] = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique[:12]


def _title_candidates(title: str, record_id: int) -> List[Any]:
    """Candidate linked records for title-similarity matching.

    Prefilters via the records_fts trigram index so candidates are actually
    title-related (the historical implementation took an arbitrary window of
    the first N rows, which stops matching anything once the DB grows).
    Falls back to that scan when FTS5 is unavailable.
    """
    conn = get_connection()
    terms = _fts_terms(title)
    if terms:
        query = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        try:
            cur = conn.execute(
                """SELECT r.record_id, rmm.master_trial_id, r.title
                   FROM records_fts
                   JOIN registry_records r ON r.record_id = records_fts.rowid
                   JOIN record_master_map rmm ON rmm.record_id = r.record_id
                   WHERE records_fts MATCH ? AND r.is_latest = 1 AND r.record_id != ?
                   LIMIT ?""",
                (query, record_id, CONFIG.er.max_candidates),
            )
            return cur.fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS candidate query failed (%s); using scan fallback", exc)

    cur = conn.execute(
        """SELECT r.record_id, rmm.master_trial_id, r.title
           FROM registry_records r
           JOIN record_master_map rmm ON rmm.record_id = r.record_id
           WHERE r.is_latest = 1 AND r.record_id != ?
           LIMIT ?""",
        (record_id, CONFIG.er.max_candidates),
    )
    return cur.fetchall()


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ── Review queue batch helpers（审核队列批处理）─────────────────────────

# confidence 分档边界（升序）：>=0.90 / [0.80, 0.90) / [0.70, 0.80) / <0.70。
# queue_stats() 与 CLI `review_queue stats/batch` 的分档输出均由该常量派生，
# 调整边界即同步生效，无需改动其它代码。
CONFIDENCE_BUCKET_EDGES: List[float] = [0.70, 0.80, 0.90]


def _confidence_bucket_labels() -> List[str]:
    """根据 CONFIDENCE_BUCKET_EDGES 生成分档标签（含两端开区间）。

    例如边界 [0.70, 0.80, 0.90] 生成：
      ["<0.70", "0.70-0.80", "0.80-0.90", ">=0.90"]
    标签顺序与 bisect 分桶下标一一对应（左闭右开，最上档为闭区间）。
    """
    labels: List[str] = []
    lower: Optional[float] = None
    for edge in CONFIDENCE_BUCKET_EDGES:
        if lower is None:
            labels.append(f"<{edge:.2f}")
        else:
            labels.append(f"{lower:.2f}-{edge:.2f}")
        lower = edge
    if lower is not None:
        labels.append(f">={lower:.2f}")
    return labels


def queue_stats() -> Dict[str, Any]:
    """统计 pending 队列按 confidence 分档的分布。

    返回 dict：
      - total:            pending 总条数
      - buckets:          {分档标签: 条数}（标签由 CONFIDENCE_BUCKET_EDGES 生成）
      - earliest_created: 最早 created_at（队列为空时为 None）
      - latest_created:   最晚 created_at（队列为空时为 None）
    """
    conn = get_connection()
    labels = _confidence_bucket_labels()
    buckets: Dict[str, int] = {label: 0 for label in labels}
    cur = conn.execute(
        "SELECT confidence FROM resolution_queue WHERE status = 'pending'"
    )
    for row in cur:
        # 左闭右开分桶：confidence 恰等于边界时归入更高一档
        idx = bisect.bisect_right(CONFIDENCE_BUCKET_EDGES, row["confidence"])
        buckets[labels[idx]] += 1
    agg = conn.execute(
        """SELECT COUNT(*) AS total,
                  MIN(created_at) AS earliest,
                  MAX(created_at) AS latest
           FROM resolution_queue WHERE status = 'pending'"""
    ).fetchone()
    return {
        "total": agg["total"],
        "buckets": buckets,
        "earliest_created": agg["earliest"],
        "latest_created": agg["latest"],
    }


def get_queue_batch(
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    limit: Optional[int] = None,
    status: str = "pending",
) -> List[Dict[str, Any]]:
    """按 confidence 区间批量取队列条目（confidence 降序）。

    区间为闭区间：min_confidence <= confidence <= max_confidence（None 表示不限）。
    limit 限制返回条数。JOIN 记录/来源表以附带 source_trial_id、title、
    source_name，便于批量审核界面直接人工确认。返回 dict 列表。
    """
    conn = get_connection()
    sql = """SELECT q.*, r.source_trial_id, r.title,
                    src.short_name as source_name
             FROM resolution_queue q
             JOIN registry_records r ON r.record_id = q.record_id
             JOIN registry_sources src ON src.source_id = r.source_id
             WHERE q.status = ?"""
    params: List[Any] = [status]
    if min_confidence is not None:
        sql += " AND q.confidence >= ?"
        params.append(min_confidence)
    if max_confidence is not None:
        sql += " AND q.confidence <= ?"
        params.append(max_confidence)
    sql += " ORDER BY q.confidence DESC, q.queue_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, tuple(params))
    return [dict(row) for row in cur.fetchall()]


def batch_resolve(
    queue_ids: list[int],
    action: str,
    reviewer: str = None,
) -> tuple[int, list]:
    """批量审批队列条目（复用单条逻辑保证审计字段一致）。

    action 仅允许 "approve" / "reject"，内部逐条调用 approve_match /
    reject_match，因此 reviewed_by / reviewed_at 及 record_master_map
    的写入与单条操作完全一致。单个条目失败（不存在、已处理或异常）
    不会中断整批。

    返回 (成功条数, [(queue_id, 失败原因), ...])。
    """
    if action not in ("approve", "reject"):
        raise ValueError(
            f"action must be 'approve' or 'reject', got: {action!r}"
        )
    resolver = approve_match if action == "approve" else reject_match
    succeeded = 0
    failures: list = []
    for qid in queue_ids:
        try:
            if resolver(qid, reviewer=reviewer):
                succeeded += 1
            else:
                failures.append((qid, "not found or already resolved"))
        except Exception as exc:  # 单条失败不中断整批
            logger.error("Batch %s failed for queue entry %d: %s",
                         action, qid, exc)
            failures.append((qid, str(exc)))
    logger.info("Batch %s: %d succeeded, %d failed (by %s)",
                action, succeeded, len(failures), reviewer)
    return succeeded, failures


def export_queue_csv(
    path: str,
    min_confidence: Optional[float] = None,
    status: str = "pending",
) -> int:
    """导出队列条目到 CSV 文件，返回数据行数（不含表头）。

    使用 utf-8-sig 编码（写入 BOM），Excel 可直接打开不乱码。
    列覆盖审核所需字段：队列/记录/来源/建议主记录/方法/置信度/理由等。
    """
    items = get_queue_batch(min_confidence=min_confidence, status=status)
    fieldnames = [
        "queue_id", "record_id", "source_name", "source_trial_id", "title",
        "suggested_master_id", "match_method", "confidence",
        "reasoning", "status", "created_at",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({key: item.get(key, "") for key in fieldnames})
    logger.info("Exported %d queue entries (%s) to %s",
                len(items), status, path)
    return len(items)
