#!/usr/bin/env python3
"""通用疾病验证案例 runner —— 一条命令跑通「NCT 概念检索 → 中文源发现/增强 →
实体解析 → HTML 报告 → 扁平化导出」，全部产物隔离在独立目录，不触碰生产库。

替代此前的 heart_valve/run_case.py 与 myocarditis/run_case.py（两者 ~80%
重复；2026-09-08 心肌炎验证后的流程优化项之一）。

用法示例（心肌炎案例的完整复现）::

    python scripts/disease_case.py --key myocarditis \
        --label 心肌炎 --label-en "Myocarditis" \
        --query "AREA[StartDate]RANGE[2024-01-01,MAX] AND (myocarditis OR myocardial inflammation)" \
        --keywords "心肌炎,Myocarditis,myocarditis,myocardial inflammation,炎性心肌病,inflammatory cardiomyopathy" \
        --old-baseline-keyword 心肌炎

输出目录结构（PROJECT/<key>/，runtime 产物请加入 .gitignore）：
    database/<key>_trials.db  raw/  reports/<key>_report_*.html
    normalized/records.json|csv  run_summary.json  logs/run.log

中文源阶段（默认开启，可 --skip-chictr/--skip-ctr 跳过）走发现层三段式：
列表倒序走水位线 → discovery_queue → 预算内详情增强；并在 ChiCTR 上实测
一次「旧机制基线」（标题关键词检索命中总数，1 次请求），写入 run_summary
供新旧机制覆盖率对照。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="通用疾病验证案例 runner（隔离运行，不触碰生产库）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--key", required=True,
                   help="疾病 profile 键，同时是输出目录名（如 myocarditis）")
    p.add_argument("--label", required=True, help="中文标签（报告标题用）")
    p.add_argument("--label-en", dest="label_en", required=True,
                   help="英文标签")
    p.add_argument("--query", required=True,
                   help="NCT query 检索式（疾病主题建议 cond 区域 + AREA[StartDate]RANGE 时间窗）")
    p.add_argument("--nct-search-area", dest="nct_search_area", default="cond",
                   choices=["cond", "term", "title", "intr", "outc"],
                   help="NCT 检索区域：cond=适应症（疾病主题默认）；term=全字段"
                        "（生物标志物/自身抗体等「非疾病型」主题——概念词不在"
                        "疾病字段里，query.cond 恒为 0 命中，须用 term）")
    p.add_argument("--keywords", required=True,
                   help="报告关键词，逗号分隔（中英文均可，用于命中统计）")
    p.add_argument("--nct-cap", type=int, default=1500,
                   help="NCT 抓取条数上限（0=不限）")
    p.add_argument("--chictr-enrich", type=int, default=100,
                   help="ChiCTR 详情增强条数（0=跳过增强，仅发现）")
    p.add_argument("--ctr-enrich", type=int, default=50,
                   help="CTR 详情增强条数（0=跳过增强，仅发现）")
    p.add_argument("--old-baseline-keyword", default=None,
                   help="旧机制基线：ChiCTR 标题检索该词的命中总数（默认取第一个中文关键词）")
    p.add_argument("--blind-enrich", action="store_true",
                   help="禁用关键词精准增强（默认把 --keywords 传给 enrich 阶段，"
                        "标题命中的待增强行优先消耗预算）")
    p.add_argument("--skip-nct", action="store_true", help="跳过 NCT 抓取")
    p.add_argument("--skip-chictr", action="store_true", help="跳过 ChiCTR 阶段")
    p.add_argument("--skip-ctr", action="store_true", help="跳过 CTR 阶段")
    p.add_argument("--skip-resolve", action="store_true", help="跳过实体解析")
    p.add_argument("--skip-report", action="store_true", help="跳过 HTML 报告")
    return p.parse_args()


ARGS = parse_args()
KEY = ARGS.key
OUT = PROJECT / KEY

# ── 隔离重定向（必须在导入 db/collectors/ct_report 之前完成） ──────────────
import config  # noqa: E402

for _sub in ("database", "logs", "reports", "normalized", "raw"):
    (OUT / _sub).mkdir(parents=True, exist_ok=True)
config.CONFIG.db.path = OUT / "database" / f"{KEY}_trials.db"
config.CONFIG.log.file = OUT / "logs" / "run.log"
config.RAW_JSON_DIR = OUT / "raw"
config.DATA_DIR = OUT / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(OUT / "logs" / "run.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger(KEY)

KEYWORDS = [k.strip() for k in ARGS.keywords.split(",") if k.strip()]
LATIN_KWS = [k.lower() for k in KEYWORDS if k.isascii()]
CJK_KWS = [k for k in KEYWORDS if not k.isascii()]

# ── 注入疾病 profile（ct_report.constants 在导入时读取，故必须先行 patch） ──
config.DISEASE_PROFILES[KEY] = {
    "label": ARGS.label, "label_en": ARGS.label_en,
    "query_cond": ARGS.query, "report_keywords": KEYWORDS,
}
config.ACTIVE_PROFILE_KEY = KEY
config.ACTIVE_PROFILE = config.DISEASE_PROFILES[KEY]

_nct = config.CONFIG.sources["clinicaltrials_gov"]
_nct.extra = dict(_nct.extra, query_cond=ARGS.query,
                  nct_search_area=ARGS.nct_search_area)
_nct.max_records_per_run = ARGS.nct_cap
_nct.enabled = True


def step_nct_crawl() -> float:
    from db.schema import create_schema
    from db.connection import get_connection
    from run_monitor import cmd_crawl

    t0 = time.time()
    create_schema()
    conn = get_connection()
    conn.execute("UPDATE registry_sources SET enabled=1 WHERE short_name='NCT'")
    conn.commit()
    cmd_crawl(argparse.Namespace(
        source="clinicaltrials_gov", incremental=False,
        bootstrap=False, query_cond=None,
    ))
    return time.time() - t0


def _matched_count(conn: sqlite3.Connection, source_short: str) -> int:
    """本地全字段（标题/科学标题/适应症）疾病关键词命中数——新机制口径。"""
    conds = ["s.short_name = ?", "r.is_latest = 1"]
    params: list = [source_short]
    for kw in LATIN_KWS:
        for col in ("r.title", "r.scientific_title", "r.conditions"):
            conds.append(f"lower(COALESCE({col},'')) LIKE ?")
            params.append(f"%{kw.lower()}%")
    for kw in CJK_KWS:
        for col in ("r.title", "r.scientific_title", "r.conditions"):
            conds.append(f"COALESCE({col},'') LIKE ?")
            params.append(f"%{kw}%")
    cur = conn.execute(
        f"SELECT COUNT(*) FROM registry_records r "
        f"JOIN registry_sources s USING(source_id) "
        f"WHERE ({' OR '.join(conds[2:])}) "
        f"AND ({' AND '.join(conds[:2])})",
        params,
    )
    return cur.fetchone()[0]


def step_chinese_sources() -> dict:
    from collectors.chictr import ChiCTRCollector
    from collectors.chinadrugtrials import ChinaDrugTrialsCollector

    out: dict = {}
    enrich_kws = None if ARGS.blind_enrich else KEYWORDS
    if not ARGS.skip_chictr:
        chi = ChiCTRCollector()
        t0 = time.time()
        d = chi.discover_new()
        out["chictr_discover"] = {k: d[k] for k in
                                  ("queued", "pages_walked", "stopped_reason")}
        if ARGS.chictr_enrich:
            e = chi.enrich_pending(limit=ARGS.chictr_enrich, keywords=enrich_kws)
            out["chictr_enrich"] = {k: e[k] for k in
                                    ("enriched", "failed", "skipped")
                                    if k in e}
            if "keyword_matched" in e:
                out["chictr_enrich"]["keyword_matched"] = e["keyword_matched"]
        out["chictr_seconds"] = round(time.time() - t0, 1)
        baseline_kw = ARGS.old_baseline_keyword or (CJK_KWS[0] if CJK_KWS else None)
        if baseline_kw:
            try:
                out["chictr_old_keyword_total"] = chi._search_total_count(baseline_kw)
                out["chictr_old_keyword_term"] = baseline_kw
            except Exception as exc:
                out["chictr_old_keyword_total"] = f"failed: {exc!r}"

    if not ARGS.skip_ctr:
        ctr = ChinaDrugTrialsCollector()
        t0 = time.time()
        d2 = ctr.discover_new()
        out["ctr_discover"] = {k: d2[k] for k in
                               ("queued", "pages_walked", "stopped_reason")}
        if ARGS.ctr_enrich:
            e2 = ctr.enrich_pending(limit=ARGS.ctr_enrich, keywords=enrich_kws)
            out["ctr_enrich"] = {k: e2[k] for k in
                                 ("enriched", "failed", "skipped") if k in e2}
            if "keyword_matched" in e2:
                out["ctr_enrich"]["keyword_matched"] = e2["keyword_matched"]
        out["ctr_seconds"] = round(time.time() - t0, 1)
    return out


def step_resolve() -> float:
    from run_monitor import cmd_resolve

    t0 = time.time()
    cmd_resolve(argparse.Namespace())
    return time.time() - t0


def step_report() -> tuple[float, str]:
    from ct_report.report import generate_report

    t0 = time.time()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT / "reports" / f"{KEY}_report_{ts}.html"
    generate_report(output=str(out), force=True, keywords=KEYWORDS,
                    english=True, update_checkpoint=False)
    return time.time() - t0, str(out)


def step_export() -> dict:
    from db.connection import get_connection

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT r.record_id, s.short_name AS source, r.source_trial_id,
               r.title, r.study_phase, r.enrollment, r.registration_date,
               r.start_date, r.source_url, r.conditions, r.sponsors,
               rmm.master_trial_id, st.label AS status
        FROM registry_records r
        JOIN registry_sources s USING(source_id)
        LEFT JOIN record_master_map rmm USING(record_id)
        LEFT JOIN status_types st ON st.status_type_id = r.status_id
        WHERE r.is_latest = 1
        ORDER BY s.short_name, r.source_trial_id
    """).fetchall()
    records = [dict(row) for row in rows]

    (OUT / "normalized" / "records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = list(records[0].keys()) if records else ["record_id"]
    with (OUT / "normalized" / "records.csv").open(
            "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)

    by_source: dict[str, int] = {}
    for rec in records:
        by_source[rec["source"]] = by_source.get(rec["source"], 0) + 1
    sources = sorted(by_source) or ["NCT", "ChiCTR", "CTR"]
    conn2 = get_connection()
    return {
        "query_cond": ARGS.query,
        "keywords": KEYWORDS,
        "latest_records": len(records),
        "records_per_source": by_source,
        f"{KEY}_hits_per_source": {
            src: _matched_count(conn2, src) for src in sources
        },
        "pending_discovery_queue": conn2.execute(
            "SELECT COUNT(*) FROM discovery_queue WHERE state='pending'"
        ).fetchone()[0],
    }


def main() -> None:
    from db.schema import create_schema

    t_start = time.time()
    log.info("disease case start: key=%s out=%s", KEY, OUT)
    create_schema()  # 幂等；跳过抓取阶段时导出/解析也需要表在位

    summary: dict = {}
    if not ARGS.skip_nct:
        summary["nct_crawl_seconds"] = round(step_nct_crawl(), 1)
    if not ARGS.skip_chictr or not ARGS.skip_ctr:
        summary["chinese_sources"] = step_chinese_sources()
    if not ARGS.skip_resolve:
        summary["resolve_seconds"] = round(step_resolve(), 1)

    report_path = None
    if not ARGS.skip_report:
        report_s, report_path = step_report()
        summary["report_seconds"] = round(report_s, 1)

    summary.update(step_export())
    summary.update({
        "total_seconds": round(time.time() - t_start, 1),
        "report_file": report_path,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })
    (OUT / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("summary: %s", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
