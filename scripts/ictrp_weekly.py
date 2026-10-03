#!/usr/bin/env python3
"""WHO ICTRP 快照周任务：fetch → import → diff → discovery_queue。

the project design notes §2.1（Agent-C，L0 补底）：
把 scripts/fetch_ictrp_xml.py（门户抓取）与 scripts/import_ictrp_xml.py
（快照导入）串成可调度的周任务，并对快照做「diff 对账」——

  1. fetch：按疾病 profile 抓取 trialsearch.who.int XML 导出（--skip-fetch
     复用本地快照，离线/调度器已预取时使用）。
  2. import：复用 import_ictrp_xml.import_snapshot 把快照 upsert 进库
     （ICTRP 仍是 AGGREGATOR，is_bootstrap=1，不计独立发现）。
  3. diff：快照中 primary source（<reg_name> 映射）为 ChiCTR / CTR 的记录，
     提取源生注册号，查 registry_records JOIN registry_sources 判断该号是否
     已有本源记录；没有 → INSERT OR IGNORE 写 discovery_queue
     (discovered_via='ictrp_diff', state='pending')。详情键（ChiCTR
     proj_id / CTR uuid）由采集器 enrich 侧自帔回查逻辑消费，本脚本只入队。
  4. 水位：discovery_cursors 写 mode='ictrp_diff'（UNIQUE(source_id, mode)
     UPSERT），cursor_json 记快照文件名/记录数/入队数/时间。
  5. 汇总：输出 JSON（imported/queued/already_present/per_profile）；
     --dry-run（默认）只统计、不写库。

用法：
    python scripts/ictrp_weekly.py --apply                     # 全部 profile
    python scripts/ictrp_weekly.py --profiles mi,hf --apply    # 指定 profile
    python scripts/ictrp_weekly.py --skip-fetch --xml data/ictrp_mi_export.xml
    python scripts/ictrp_weekly.py                             # dry-run 默认
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:  # 既支持包内导入（pytest），也支持直接 python scripts/ictrp_weekly.py
    from scripts.import_ictrp_xml import import_snapshot
    from scripts import fetch_ictrp_xml
except ImportError:  # pragma: no cover - 直接执行时的兜底路径
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from import_ictrp_xml import import_snapshot  # type: ignore
    import fetch_ictrp_xml  # type: ignore

from collectors.who_ictrp import WHOICTRPCollector
from config import DISEASE_PROFILES, PROJECT_ROOT
from db.connection import get_connection

# ── 常量 ──────────────────────────────────────────────────────────────────────

#: 快照 <reg_name>（小写归一）→ 本库 registry_sources.short_name。
#: 仅收两个中文主注册源；其余 reg_name（含 ClinicalTrials.gov）不是本任务
#: 的 diff 对象，跳过并计数。目标 short_name 与 db/schema.py 种子一致：
#: ChiCTR（中国临床试验注册中心）、CTR（中国药物临床试验登记与信息公示平台）。
REGNAME_TO_SOURCE: Dict[str, str] = {
    # ChiCTR
    "chictr": "ChiCTR",
    "chictr.org": "ChiCTR",
    "chinese clinical trial register": "ChiCTR",
    "chinese clinical trial registry": "ChiCTR",
    # CTR / CDE 平台
    "中国药物临床试验登记与信息公示平台": "CTR",
    "药物临床试验登记与信息公示平台": "CTR",
    "cde": "CTR",
    "chinadrugtrials": "CTR",
    "chinadrugtrials.org.cn": "CTR",
    "china drug trials": "CTR",
    "chinese drug trials": "CTR",
}

#: diff 的目标源（须与 db/schema.py 种子 short_name 完全一致）。
DIFF_TARGET_SOURCES = ("ChiCTR", "CTR")

#: discovery_cursors 里本任务的 mode 名（与方案 §2.4 枚举一致）。
CURSOR_MODE = "ictrp_diff"


# ── profile / 路径解析 ────────────────────────────────────────────────────────


def resolve_profiles(spec: str) -> List[str]:
    """把 --profiles 参数解析为合法 profile 键列表。

    ``all``（大小写不敏感）→ 全部 DISEASE_PROFILES 键（排序保证确定性）；
    逗号列表逐个校验，未知键抛 ValueError（main 转 parser.error）。
    """
    spec = (spec or "all").strip()
    if spec.lower() == "all":
        return sorted(DISEASE_PROFILES)
    keys = [k.strip() for k in spec.split(",") if k.strip()]
    if not keys:
        raise ValueError("empty --profiles value")
    unknown = [k for k in keys if k not in DISEASE_PROFILES]
    if unknown:
        raise ValueError(
            f"unknown profile(s): {', '.join(unknown)} — "
            f"known: {', '.join(sorted(DISEASE_PROFILES))}"
        )
    return keys


def snapshot_path_for(profile_key: str) -> Path:
    """按 config 约定派生快照路径 data/ictrp_<profile>_export.xml。

    与 config.py 的 ``who_ictrp.extra.xml_export_path`` 及
    fetch_ictrp_xml._profile_defaults 的落盘路径保持一致。
    """
    return PROJECT_ROOT / "data" / f"ictrp_{profile_key}_export.xml"


def profile_query(profile: dict) -> str:
    """取 profile 的英文检索词（第一个 ASCII 关键词，兜底 label_en）。

    与 fetch_ictrp_xml._profile_defaults 的取词逻辑一致，供 fetch 步使用。
    """
    kw = next((k for k in profile["report_keywords"] if k.isascii()), None)
    return kw or profile["label_en"]


def fetch_snapshot(profile_key: str, xml_path: Path) -> bool:
    """网络步：抓取 ICTRP XML 导出（薄封装，便于测试 monkeypatch）。"""
    return fetch_ictrp_xml.fetch_export(
        profile_query(DISEASE_PROFILES[profile_key]), xml_path
    )


def map_reg_name(reg_name: Optional[str]) -> Optional[str]:
    """<reg_name> → 目标源 short_name；不在映射内返回 None（跳过并计数）。"""
    if not reg_name:
        return None
    return REGNAME_TO_SOURCE.get(reg_name.strip().lower())


# ── diff：快照 → discovery_queue ─────────────────────────────────────────────


def _target_source_ids(conn) -> Dict[str, int]:
    """目标源 short_name → source_id（缺种子的源告警跳过）。"""
    ids: Dict[str, int] = {}
    for short in DIFF_TARGET_SOURCES:
        row = conn.execute(
            "SELECT source_id FROM registry_sources WHERE short_name = ?",
            (short,),
        ).fetchone()
        if row is None:
            print(f"WARNING: registry_sources 缺少 {short}，该源本轮跳过")
            continue
        ids[short] = row["source_id"]
    return ids


def _source_has_trial(conn, source_id: int, source_trial_id: str) -> bool:
    """该源是否已有此注册号（registry_records JOIN registry_sources）。"""
    row = conn.execute(
        """
        SELECT r.record_id
        FROM registry_records r
        JOIN registry_sources s ON s.source_id = r.source_id
        WHERE s.source_id = ? AND r.source_trial_id = ?
        LIMIT 1
        """,
        (source_id, source_trial_id),
    ).fetchone()
    return row is not None


def diff_snapshot(records: List[Dict[str, Any]], apply: bool = False) -> dict:
    """对快照记录做 diff 对账，返回统计（apply=True 才真正写 discovery_queue）。

    判定：reg_name 映射到 ChiCTR/CTR 的记录，取源生注册号 <trial_id>；
    该 (source_id, source_trial_id) 在 registry_records 无本源记录 → 新发现，
    INSERT OR IGNORE 入队（UNIQUE(source_id, source_trial_id) 保证幂等，
    第二遍跑同快照 queued=0）。未知 reg_name 跳过并计数。
    """
    conn = get_connection()
    source_ids = _target_source_ids(conn)

    stats = {
        "scanned": 0,
        "queued": 0,
        "already_present": 0,
        "unknown_registry": 0,
        "missing_trial_id": 0,
        "queued_by_source": {short: 0 for short in source_ids},
        "queued_ids": [],
    }
    seen: set = set()  # 同快照内 (short, trial_id) 去重

    for raw in records:
        short = map_reg_name(raw.get("reg_name"))
        if short is None or short not in source_ids:
            stats["unknown_registry"] += 1
            continue
        trial_id = (raw.get("trial_id") or "").strip()
        if not trial_id:
            stats["missing_trial_id"] += 1
            continue
        key = (short, trial_id)
        if key in seen:
            continue
        seen.add(key)
        stats["scanned"] += 1

        sid = source_ids[short]
        if _source_has_trial(conn, sid, trial_id):
            stats["already_present"] += 1
            continue

        if not apply:
            stats["queued"] += 1
            stats["queued_by_source"][short] += 1
            stats["queued_ids"].append(f"{short}:{trial_id}")
            continue

        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO discovery_queue "
            "(source_id, source_trial_id, discovered_via) VALUES (?, ?, ?)",
            (sid, trial_id, CURSOR_MODE),
        )
        if conn.total_changes - before:  # UNIQUE 冲突被忽略 → 不计 queued
            stats["queued"] += 1
            stats["queued_by_source"][short] += 1
            stats["queued_ids"].append(f"{short}:{trial_id}")

    if apply:
        conn.commit()
    return stats


# ── 水位：discovery_cursors(mode='ictrp_diff') ───────────────────────────────


def save_ictrp_cursors(per_source: Dict[str, dict]) -> None:
    """把水位 UPSERT 进 discovery_cursors（UNIQUE(source_id, mode)，幂等）。

    ``per_source``：short_name → cursor_json 内容（快照文件名/记录数/入队数/
    时间等）。与采集器 _save_cursor 同款 ON CONFLICT 语义，成功轮水位前移。
    """
    conn = get_connection()
    for short in DIFF_TARGET_SOURCES:
        if short not in per_source:
            continue
        row = conn.execute(
            "SELECT source_id FROM registry_sources WHERE short_name = ?",
            (short,),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            """
            INSERT INTO discovery_cursors
                (source_id, mode, cursor_json, last_attempted,
                 last_successful, status)
            VALUES (?, ?, ?, datetime('now'), datetime('now'), 'active')
            ON CONFLICT(source_id, mode) DO UPDATE SET
                cursor_json = excluded.cursor_json,
                last_attempted = excluded.last_attempted,
                last_successful = excluded.last_successful,
                status = 'active'
            """,
            (row["source_id"], CURSOR_MODE,
             json.dumps(per_source[short], ensure_ascii=False, sort_keys=True)),
        )
    conn.commit()


# ── 周任务主流程 ──────────────────────────────────────────────────────────────


def run_weekly(profiles: str = "all", xml: Optional[str] = None,
               apply: bool = False, skip_fetch: bool = False) -> dict:
    """执行一次 ICTRP 周任务，返回汇总 dict（可 json.dumps）。

    Args:
        profiles: ``all`` 或逗号分隔的 profile 键列表。
        xml: 显式快照路径；仅支持单一 profile（多 profile 交由各自默认路径）。
        apply: False=dry-run（只统计不写库）；True=import+入队+水位。
        skip_fetch: True=跳过网络步，复用本地快照。
    """
    profile_keys = resolve_profiles(profiles)
    if xml and len(profile_keys) > 1:
        raise ValueError("--xml 只能配合单一 profile 使用")

    summary: Dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "profiles": profile_keys,
        "imported": 0,
        "queued": 0,
        "already_present": 0,
        "unknown_registry": 0,
        "per_profile": {},
        "errors": [],
    }
    # 累计各目标源的 cursor 载荷（跨 profile 聚合）
    cursors: Dict[str, dict] = {short: {
        "snapshot_files": {}, "record_counts": {}, "scanned": 0,
        "queued": 0, "already_present": 0, "unknown_registry": 0,
        "profiles": [],
    } for short in DIFF_TARGET_SOURCES}
    processed_any = False

    for key in profile_keys:
        xml_path = Path(xml) if xml else snapshot_path_for(key)
        entry: Dict[str, Any] = {
            "xml": str(xml_path),
            "fetched": False,
            "fetch_ok": None,
            "parsed": 0,
            "imported": 0,
            "import_errors": 0,
            "scanned": 0,
            "queued": 0,
            "already_present": 0,
            "unknown_registry": 0,
        }

        # 1. fetch（网络步）
        if not skip_fetch:
            entry["fetched"] = True
            entry["fetch_ok"] = fetch_snapshot(key, xml_path)
            if not entry["fetch_ok"] and not xml_path.exists():
                entry["error"] = "fetch failed and no local snapshot"
                summary["errors"].append(f"{key}: {entry['error']}")
                summary["per_profile"][key] = entry
                continue

        if not xml_path.exists():
            entry["error"] = "snapshot missing (use --skip-fetch only with an existing local snapshot)"
            summary["errors"].append(f"{key}: {entry['error']}")
            summary["per_profile"][key] = entry
            continue

        # 2. 解析（dry-run 只解析不 import；截断/损坏快照不崩整轮）
        try:
            records = WHOICTRPCollector.parse_xml(
                xml_path.read_text(encoding="utf-8"))
        except ET.ParseError as exc:
            records = None
            # 中断的下载会留下半份 XML（content-length 未收尾）。能联网
            # （非 --skip-fetch）就重抓一次再试；仍失败或离线则跳过该
            # profile 并记入 errors——绝不让单份坏快照拖垮整个周任务。
            if not skip_fetch:
                entry["fetched"] = True
                entry["fetch_ok"] = fetch_snapshot(key, xml_path)
                if entry["fetch_ok"]:
                    try:
                        records = WHOICTRPCollector.parse_xml(
                            xml_path.read_text(encoding="utf-8"))
                    except ET.ParseError as exc2:
                        exc = exc2
            if records is None:
                entry["error"] = (
                    f"snapshot unparseable (truncated?): {exc} — "
                    "re-fetch or remove the file")
                summary["errors"].append(f"{key}: {entry['error']}")
                summary["per_profile"][key] = entry
                continue
        entry["parsed"] = len(records)

        # 3. import（仅 apply；dry-run 不写库）
        if apply:
            imp = import_snapshot(xml_path, records=records)
            entry["imported"] = imp["new"] + imp["updated"]
            entry["import_errors"] = imp["errors"]

        # 4. diff → discovery_queue
        diff = diff_snapshot(records, apply=apply)
        entry.update({
            "scanned": diff["scanned"],
            "queued": diff["queued"],
            "already_present": diff["already_present"],
            "unknown_registry": diff["unknown_registry"],
        })

        # 聚合
        summary["imported"] += entry["imported"]
        summary["queued"] += diff["queued"]
        summary["already_present"] += diff["already_present"]
        summary["unknown_registry"] += diff["unknown_registry"]
        for short in DIFF_TARGET_SOURCES:
            if short not in cursors:
                continue
            cur = cursors[short]
            cur["snapshot_files"][key] = xml_path.name
            cur["record_counts"][key] = len(records)
            cur["profiles"].append(key)
            cur["scanned"] += diff["scanned"]
            cur["queued"] += diff["queued_by_source"].get(short, 0)
            cur["already_present"] += diff["already_present"]
            cur["unknown_registry"] += diff["unknown_registry"]
        processed_any = True
        summary["per_profile"][key] = entry

    # 5. 水位（仅 apply 且至少处理了一份快照；dry-run 不写）
    if apply and processed_any:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for cur in cursors.values():
            cur["updated_at"] = now
        save_ictrp_cursors(cursors)
        summary["cursors"] = cursors

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> dict:
    """CLI 入口：解析参数、跑周任务、打印汇总 JSON。返回汇总供测试/调用方。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profiles", default="all",
                        help="逗号分隔的疾病 profile 列表或 all（默认 all）")
    parser.add_argument("--xml", default=None,
                        help="显式 ICTRP XML 快照路径（缺省按 profile 派生 "
                             "data/ictrp_<profile>_export.xml；仅限单一 profile）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="apply", action="store_false",
                       help="只统计不写库（默认）")
    group.add_argument("--apply", dest="apply", action="store_true",
                       help="真正 import + 写 discovery_queue + 水位")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="跳过网络抓取，复用本地快照")
    parser.set_defaults(apply=False)
    args = parser.parse_args(argv)

    try:
        summary = run_weekly(profiles=args.profiles, xml=args.xml,
                             apply=args.apply, skip_fetch=args.skip_fetch)
    except ValueError as exc:
        parser.error(str(exc))
        raise SystemExit(2)  # pragma: no cover - parser.error 必退出

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    raise SystemExit(0 if not main()["errors"] else 1)
