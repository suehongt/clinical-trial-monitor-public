"""
Digest — 每日「新增试验 + 字段变更」摘要推送。

每天 pipeline 报告步骤之后运行：把自上次成功推送以来的新试验（按疾病
profile 分组）与字段级变更事件（带 old→new 值）格式化为一条摘要，经
core.notify 五渠道（企微/钉钉/飞书/webhook/SMTP）扇出。

为什么自建水位线：trial_events.acknowledged 在整个代码库中无人置 1
（acknowledge_event 无调用方），事件去重改用自增 event_id 水位——
单调可靠，不受时钟偏移影响；新试验用水位 last_run_at 时间戳。

推送语义：
  - 渠道全未配置 → no-op 成功返回，水位不动（配置好后补看，历史不丢）；
  - 至少一个渠道发送成功 → 推进水位（避免成功渠道收到重复）；
  - 全部渠道失败 → 水位不动，下次运行自动重推。

环境变量同 core.notify（CT_NOTIFY_*）。

公开 API::

    run_digest(dry_run=False, english=False, limit=500) -> dict
    collect_new_trials(since, limit=200) -> list[dict]
    collect_change_events(since_event_id, limit=500) -> list[dict]
    format_digest(...) -> tuple[str, str]
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import DISEASE_PROFILES
from core.change_detection import FIELD_LABELS
from core.notify import configured_channels, send_notification
from db.connection import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "run_digest",
    "collect_new_trials",
    "collect_change_events",
    "format_digest",
    "digest_watermark",
]

# 摘要行数上限（超出只报数量，避免消息超长被 IM 截断）
NEW_PER_PROFILE = 5
CHANGES_PER_PROFILE = 10
VALUE_MAX_CHARS = 40
TITLE_MAX_CHARS = 70

# 首次运行（无水位）时新试验的回看窗口
FIRST_RUN_HOURS = 24


# ── 水位线 ──────────────────────────────────────────────────────────────


def digest_watermark() -> Dict[str, Any]:
    """读取推送水位线 {last_event_id, last_run_at}（无行则归零）。"""
    conn = get_connection()
    row = conn.execute(
        "SELECT last_event_id, last_run_at FROM digest_state WHERE id = 1"
    ).fetchone()
    if row:
        return {"last_event_id": row["last_event_id"] or 0,
                "last_run_at": row["last_run_at"]}
    return {"last_event_id": 0, "last_run_at": None}


def _advance_watermark(last_event_id: int) -> None:
    """推送成功后落盘水位（单行表，INSERT OR REPLACE 幂等）。"""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO digest_state (id, last_event_id, last_run_at) "
        "VALUES (1, ?, datetime('now'))",
        (last_event_id,),
    )
    conn.commit()


# ── 选取 ────────────────────────────────────────────────────────────────


def collect_new_trials(since: Optional[str], limit: int = 200, conn=None) -> List[Dict[str, Any]]:
    """取水位之后新登记的试验（新试验口径，与 core/reporting.py 一致）。

    口径：first_crawled_at > since AND is_bootstrap = 0（Bootstrap/ICTRP
    导入不计入发现）AND version_number = 1（新版本行有自己的
    first_crawled_at，靠版本号排除）AND is_latest = 1。
    ``since`` 为 None（首跑无水位）时默认回看 24 小时。

    conn: 可传入现有 sqlite 连接（API server 每请求新建连接）；默认
    db.connection.get_connection()。
    """
    if since:
        where = "r.first_crawled_at > ?"
        params: list = [since]
    else:
        where = "r.first_crawled_at > datetime('now', ?)"
        params = [f"-{FIRST_RUN_HOURS} hours"]
    c = conn if conn is not None else get_connection()
    rows = c.execute(
        f"""
        SELECT r.record_id, r.source_trial_id, r.title, r.conditions,
               r.enrollment, r.study_phase, r.source_url,
               r.first_crawled_at, s.short_name
        FROM registry_records r
        JOIN registry_sources s ON s.source_id = r.source_id
        WHERE {where}
          AND r.is_bootstrap = 0
          AND r.version_number = 1
          AND r.is_latest = 1
        ORDER BY r.first_crawled_at DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]


def collect_change_events(since_event_id: int, limit: int = 500) -> List[Dict[str, Any]]:
    """取水位之后的字段级变更事件（join 出显示上下文）。

    注意不按 r.is_latest 过滤：同一试验一天内连改两次时，中间版本的
    事件同样真实，is_latest 过滤会把它们静默吞掉。
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT te.event_id, te.field_name, te.old_value, te.new_value,
               te.change_category, te.detected_at,
               r.source_trial_id, r.title, r.conditions, r.source_url,
               s.short_name
        FROM trial_events te
        JOIN registry_records r ON r.record_id = te.record_id
        JOIN registry_sources s ON s.source_id = r.source_id
        WHERE te.event_id > ?
          AND te.event_type = 'field_change'
        ORDER BY te.event_id ASC
        LIMIT ?
        """,
        (since_event_id, limit),
    ).fetchall()
    events = [dict(r) for r in rows]
    _humanise_status_events(events)
    return events


def _status_label_map() -> Dict[str, str]:
    """{status_type_id 字符串: 状态名}——status_id 事件值翻译用。"""
    conn = get_connection()
    rows = conn.execute("SELECT status_type_id, label FROM status_types").fetchall()
    return {str(r["status_type_id"]): r["label"] for r in rows}


def _humanise_status_events(events: List[Dict[str, Any]]) -> None:
    """把 status_id 事件的整型 FK 值原位翻译为状态名（查不到保留原值）。"""
    if not any(e["field_name"] == "status_id" for e in events):
        return
    labels = _status_label_map()
    for e in events:
        if e["field_name"] != "status_id":
            continue
        for key in ("old_value", "new_value"):
            raw = e[key]
            if raw is not None and str(raw) in labels:
                e[key] = labels[str(raw)]


# ── profile 分组 ────────────────────────────────────────────────────────


def _kw_matches(text: Optional[str], kw: str) -> bool:
    """与 ct_report.query 同款语义：CJK 关键词安全子串，ASCII 词边界。"""
    if not text:
        return False
    t = text.lower()
    k = kw.lower()
    if re.search(r"[一-鿿]", k):
        return k in t
    return re.search(r"(?:\b|_)" + re.escape(k) + r"(?:\b|_)", t) is not None


def match_profile(title: Optional[str], conditions: Optional[str]) -> Optional[str]:
    """返回命中的疾病 profile key（title/conditions 命中任一关键词）；无命中 None。"""
    for key, profile in DISEASE_PROFILES.items():
        for kw in profile["report_keywords"]:
            if _kw_matches(title, kw) or _kw_matches(conditions, kw):
                return key
    return None


def _group_by_profile(items: List[Dict[str, Any]]) -> Dict[Optional[str], List[Dict[str, Any]]]:
    """按疾病 profile 分组；键为 profile key，未命中归 None（「其他」）。"""
    grouped: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for item in items:
        key = match_profile(item.get("title"), item.get("conditions"))
        grouped.setdefault(key, []).append(item)
    return grouped


# ── 格式化（纯函数） ────────────────────────────────────────────────────


def _format_value(val: Optional[str]) -> str:
    """压缩显示值：JSON 数组展开拼接，超长截断。"""
    if val is None or val == "":
        return "（空）"
    try:
        parsed = json.loads(val)
        if isinstance(parsed, list):
            val = "、".join(
                v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                for v in parsed
            )
        elif isinstance(parsed, dict):
            val = json.dumps(parsed, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    val = str(val).strip()
    if len(val) > VALUE_MAX_CHARS:
        val = val[:VALUE_MAX_CHARS] + "…"
    return val


def _format_change_line(event: Dict[str, Any], english: bool) -> str:
    """单条变更行：`[NCT] NCT123 入组人数: 120 → 240`。"""
    lang = "en" if english else "zh"
    label = FIELD_LABELS.get(event["field_name"], {}).get(lang, event["field_name"])
    old = _format_value(event.get("old_value"))
    new = _format_value(event.get("new_value"))
    title = event.get("title") or ""
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS] + "…"
    return (f"· [{event['short_name']}] {event['source_trial_id']} {label}: "
            f"{old} → {new}")


def _format_new_line(trial: Dict[str, Any]) -> str:
    """单条新增行：`· [NCT] NCT123 分期3 | 标题`。"""
    title = (trial.get("title") or "").strip()
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS] + "…"
    phase = (trial.get("study_phase") or "").strip()
    head = f"· [{trial['short_name']}] {trial['source_trial_id']}"
    if phase:
        head += f" {phase}"
    return f"{head} | {title}" if title else head


def format_digest(
    new_by_profile: Dict[Optional[str], List[Dict[str, Any]]],
    changes_by_profile: Dict[Optional[str], List[Dict[str, Any]]],
    english: bool = False,
) -> tuple[str, str]:
    """把分组结果格式化为 (title, text)；无内容时 text 为空串。"""
    now = datetime.now(timezone.utc)
    if english:
        title = f"Clinical Trial Digest · {now.strftime('%m-%d')}"
        new_hdr, chg_hdr, other = "New", "Changed", "Other"
        suffix = "…and {n} more"
    else:
        title = f"临床试验监测日报 · {now.strftime('%m-%d')}"
        new_hdr, chg_hdr, other = "新增", "变更", "其他"
        suffix = "…另有 {n} 项"

    sections: List[str] = []

    def _profile_label(key: Optional[str]) -> str:
        if key is None:
            return other
        p = DISEASE_PROFILES[key]
        return p["label_en"] if english else p["label"]

    all_keys = [k for k in list(DISEASE_PROFILES) + [None]
                if new_by_profile.get(k) or changes_by_profile.get(k)]
    for key in all_keys:
        news = new_by_profile.get(key, [])
        changes = changes_by_profile.get(key, [])
        lines = [f"【{_profile_label(key)}】"
                 f"{new_hdr} {len(news)} · {chg_hdr} {len(changes)}"]
        if news:
            lines.append(f"  {new_hdr}:")
            lines.extend(f"  {_format_new_line(t)}" for t in news[:NEW_PER_PROFILE])
            if len(news) > NEW_PER_PROFILE:
                lines.append("  " + suffix.format(n=len(news) - NEW_PER_PROFILE))
        if changes:
            lines.append(f"  {chg_hdr}:")
            lines.extend(f"  {_format_change_line(e, english)}"
                         for e in changes[:CHANGES_PER_PROFILE])
            if len(changes) > CHANGES_PER_PROFILE:
                lines.append("  " + suffix.format(n=len(changes) - CHANGES_PER_PROFILE))
        sections.append("\n".join(lines))

    if not sections:
        return title, ""
    return title, "\n\n".join(sections)


# ── 主流程 ──────────────────────────────────────────────────────────────


def run_digest(
    dry_run: bool = False,
    english: bool = False,
    limit: int = 500,
) -> Dict[str, Any]:
    """收集 → 格式化 → 推送 →（成功后）推进水位。

    返回 {status, new, changed, title, text, results?}，status 取值：
      pushed / push_failed / skipped_no_channels / dry_run / empty
    """
    # 幂等：确保 v8 水位表存在（每日流水线在此自动完成 schema 升级）
    from db.schema import create_schema
    create_schema()

    wm = digest_watermark()
    new_trials = collect_new_trials(wm["last_run_at"])
    events = collect_change_events(wm["last_event_id"], limit=limit)

    new_by_profile = _group_by_profile(new_trials)
    changes_by_profile = _group_by_profile(events)
    title, text = format_digest(new_by_profile, changes_by_profile, english)
    n_new = len(new_trials)
    n_changed = len(events)

    result: Dict[str, Any] = {
        "status": "empty", "new": n_new, "changed": n_changed,
        "title": title, "text": text,
    }
    if not text:
        logger.info("digest: no updates since watermark %s", wm)
        return result

    if dry_run:
        result["status"] = "dry_run"
        return result

    channels = configured_channels()
    if not channels:
        logger.info("digest: no CT_NOTIFY_* channels configured — "
                    "skipping push (watermark untouched)")
        result["status"] = "skipped_no_channels"
        return result

    results = send_notification(title, text, level="info")
    result["results"] = results
    if any(v == "ok" for v in results.values()):
        max_event_id = max((e["event_id"] for e in events), default=wm["last_event_id"])
        _advance_watermark(max_event_id)
        result["status"] = "pushed"
        logger.info("digest pushed: %d new, %d changed via %s",
                    n_new, n_changed,
                    [k for k, v in results.items() if v == "ok"])
    else:
        result["status"] = "push_failed"
        logger.warning("digest push failed on all channels %s — watermark untouched", results)
    return result
