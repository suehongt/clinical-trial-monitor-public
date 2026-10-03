"""
Refresh queue — periodic re-verification of already-enriched ChiCTR/CTR records.

The discovery layer (discover → discovery_queue → enrich) only handles
*newly registered* trial numbers: the list-page watermark and the ICTRP
snapshot diff both deliberately skip IDs already present in
registry_records.  Field updates on existing records (e.g. a sponsor
raising the target enrollment) therefore go unnoticed for the Chinese
registries, whose list pages carry no update timestamps to drive an
incremental fetch.

The refresh queue closes that gap by re-visiting detail pages of known
records, oldest ``last_crawled_at`` first, within the same WAF budget as
enrichment (``enrich_batch_size``, G2: ≤50 detail requests per source per
round).  Re-fetching is safe by construction: ``_upsert_record`` hash-skips
unchanged content (touching ``last_crawled_at`` only, no spurious version)
and emits trial_events when content did change.

Sources plug in a single hook — ``collector.refresh_one(source_trial_id)``
returning ``{"action": "new"|"updated"|"skipped", "changes": int[, "stub"]}``
— everything queue-shaped (selection, retries, WAF-circuit semantics)
lives here so the two collectors stay symmetric.

Operational policy (unchanged from the discovery layer): manual off-peak
runs, one round per source per night; ``WafCircuitOpenError`` aborts the
round and leaves pending rows for the next one.

CLI::

    python run_monitor.py crawl --refresh-only [--refresh-limit 50]

公开 API::

    plan_refresh(batch=200, stale_days=30, source_short_names=...) -> int
    refresh_pending(collector, limit=None) -> dict
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from collectors.browser_base import WafCircuitOpenError
from db.connection import get_connection

logger = logging.getLogger(__name__)

# 失败重试上限：与发现队列 ENRICH_MAX_ATTEMPTS 同款语义
REFRESH_MAX_ATTEMPTS = 3

DEFAULT_STALE_DAYS = 30
DEFAULT_PLAN_BATCH = 200

__all__ = [
    "plan_refresh",
    "refresh_pending",
    "refresh_queue_stats",
]


# ── 入队 ────────────────────────────────────────────────────────────────


def plan_refresh(
    batch: int = DEFAULT_PLAN_BATCH,
    stale_days: int = DEFAULT_STALE_DAYS,
    source_short_names: Sequence[str] = ("ChiCTR", "CTR"),
) -> int:
    """把「久未重访」的本源最新版记录批量入复查队列，返回本轮入队数。

    选取口径：is_latest=1 且 last_crawled_at 早于 stale_days 天，按
    last_crawled_at 升序（最久未验证者优先）。部分唯一索引
    idx_refresh_queue_pending 保证同一试验同时至多一条 pending，
    INSERT...SELECT 天然幂等——已 pending 的行不会重复入队。

    done 行保留为「上次复查时间」历史，不入队条件由 NOT EXISTS(pending)
    与 last_crawled_at（复查会刷新它）共同保证。
    """
    # 幂等：确保 v8 队列表存在（手动 --refresh-only 首跑即自动升级 schema）
    from db.schema import create_schema
    create_schema()

    conn = get_connection()
    placeholders = ", ".join("?" for _ in source_short_names)
    cur = conn.execute(
        f"""
        INSERT INTO refresh_queue (source_id, source_trial_id, record_id)
        SELECT r.source_id, r.source_trial_id, r.record_id
        FROM registry_records r
        JOIN registry_sources s ON s.source_id = r.source_id
        WHERE s.short_name IN ({placeholders})
          AND r.is_latest = 1
          AND r.last_crawled_at < datetime('now', ?)
          AND NOT EXISTS (
              SELECT 1 FROM refresh_queue q
              WHERE q.source_id = r.source_id
                AND q.source_trial_id = r.source_trial_id
                AND q.state = 'pending'
          )
        ORDER BY r.last_crawled_at ASC
        LIMIT ?
        """,
        (*source_short_names, f"-{stale_days} days", batch),
    )
    conn.commit()
    queued = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    logger.info("plan_refresh: queued=%d (stale_days=%d, batch=%d, sources=%s)",
                queued, stale_days, batch, list(source_short_names))
    return queued


# ── 消费 ────────────────────────────────────────────────────────────────


def _mark_refresh(refresh_id: int, state: str) -> None:
    """复查行终态：done / skipped（清除 last_error）。"""
    conn = get_connection()
    conn.execute(
        "UPDATE refresh_queue SET state = ?, last_error = NULL, "
        "done_at = datetime('now') WHERE refresh_id = ?",
        (state, refresh_id),
    )
    conn.commit()


def _mark_refresh_failed_attempt(refresh_id: int, error: str) -> None:
    """失败尝试：attempts+1 并记 last_error；达上限转 failed。"""
    conn = get_connection()
    conn.execute(
        "UPDATE refresh_queue SET attempts = attempts + 1, "
        "last_error = ?, done_at = CASE "
        "WHEN attempts + 1 >= ? THEN datetime('now') ELSE done_at END, "
        "state = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE state END "
        "WHERE refresh_id = ?",
        ((error or "unknown error")[:1000], REFRESH_MAX_ATTEMPTS,
         REFRESH_MAX_ATTEMPTS, refresh_id),
    )
    conn.commit()


def refresh_pending(collector, limit: Optional[int] = None) -> dict:
    """按预算消化 refresh_queue 本源 pending 行（驱动器，源无关）。

    limit 缺省取 cfg.extra[enrich_batch_size]（G2：单轮 ≤50 详情请求）。
    逐行调用 collector.refresh_one(source_trial_id)：
      - 成功（含 upsert hash-skip）→ state=done；
      - 桩页（号段空穴，非错误）→ state=skipped；
      - 失败 → attempts+1、last_error 记录，attempts ≥ REFRESH_MAX_ATTEMPTS
        转 failed；
      - WafCircuitOpenError → 中止本轮，剩余 pending 留待下轮（游标不涉
        水，复查队列本身无水位线语义）。

    返回 {refreshed, failed, skipped, changes}；changes 为本轮补写的
    trial_events 数（即真正检测到的字段变更量）。
    """
    if limit is None:
        limit = ((collector.cfg.extra or {}).get("enrich_batch_size")
                 or collector.cfg.max_records_per_run or 50)
    conn = get_connection()
    rows = conn.execute(
        "SELECT refresh_id, source_trial_id FROM refresh_queue "
        "WHERE source_id = ? AND state = 'pending' "
        "ORDER BY refresh_id LIMIT ?",
        (collector.source_id, limit),
    ).fetchall()

    stats: dict = {"refreshed": 0, "failed": 0, "skipped": 0, "changes": 0}
    for row in rows:
        refresh_id = row["refresh_id"]
        trial_id = row["source_trial_id"]
        time.sleep(collector.cfg.request_delay_sec)
        try:
            outcome = collector.refresh_one(trial_id)
            if outcome.get("stub"):
                logger.info("refresh 桩页跳过 %s", trial_id)
                _mark_refresh(refresh_id, "skipped")
                stats["skipped"] += 1
                continue
            _mark_refresh(refresh_id, "done")
            stats["refreshed"] += 1
            stats["changes"] += int(outcome.get("changes") or 0)
            if outcome.get("changes"):
                logger.info("refresh 检测到变更 %s: %d 条事件",
                            trial_id, outcome["changes"])
        except WafCircuitOpenError:
            logger.error("refresh 熔断中止，剩余 pending 留待下轮")
            break
        except Exception as exc:
            logger.warning("refresh 失败 %s: %s", trial_id, exc)
            _mark_refresh_failed_attempt(refresh_id, str(exc))
            stats["failed"] += 1

    logger.info("refresh_pending(%s): %s", collector.cfg.short_name, stats)
    return stats


# ── 观测 ────────────────────────────────────────────────────────────────


def refresh_queue_stats(conn=None) -> dict:
    """按源 × 状态统计复查队列（质量报告/运维简报/统计端点用）。

    conn: 可传入现有 sqlite 连接（API server 每请求新建连接，避免触碰
    db.connection 的线程本地缓存）；默认 db.connection.get_connection()。
    """
    c = conn if conn is not None else get_connection()
    rows = c.execute(
        """
        SELECT s.short_name, q.state, COUNT(*) AS n
        FROM refresh_queue q
        JOIN registry_sources s ON s.source_id = q.source_id
        GROUP BY s.short_name, q.state
        """
    ).fetchall()
    by_source_state = {
        f"{r['short_name']}:{r['state']}": r["n"] for r in rows
    }
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["state"]] = totals.get(r["state"], 0) + r["n"]
    return {"by_source_state": by_source_state, "totals": totals}
