#!/usr/bin/env python3
"""中文源定向关键词检索工具 —— 用疾病/概念关键词直接检索 ChiCTR/CTR 的
搜索接口，把命中试验的详情页增强入库，并输出命中统计。

动机（2026-09-12 M2 案例复盘）：三段式「发现→队列→增强」的 enrich 关键词
优先只能在「发现队列已存标题」里匹配，而广谱词表走出的队列标题几乎不可能
包含疾病/概念专用词——主题型试验（如自身抗体、生物标志物）永远进不了库。
本工具对搜索接口做标题级定向检索，是发现层的关键词正交通道：
    广谱词表 list_walk     → 覆盖「最新注册」切面（disease_case 主路径）
    ICTRP 快照 diff (L0)   → 覆盖跨库兜底
    keyword_sweep（本工具）→ 覆盖「主题词直查」切面

用法（配合 disease_case 的隔离库时，先 --db 指向案例库；生产库则省略）::

    python scripts/keyword_sweep.py --keywords "毒蕈碱,M2受体,muscarinic" \
        --db m2_muscarinic_ab/database/m2_muscarinic_ab_trials.db \
        --output m2_muscarinic_ab/targeted_sweep.json

注意：
  - ChiCTR 走 HTTP 热路径（waf_http，节律地板自动生效）；
  - CTR 走 Playwright 浏览器路径（瑞数 WAF，~5s/页），站况波动时依赖
    连续失败熔断（failure_breaker_threshold）止损；
  - 入库复用采集器 enrich 的 parse → normalise → upsert 全链路，幂等。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="中文源定向关键词检索：命中详情增强入库 + 命中统计输出",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--keywords", required=True,
                   help="逗号分隔的检索词（中英文均可，按标题匹配）")
    p.add_argument("--source", choices=["chictr", "ctr", "all"], default="all",
                   help="检索的中文源")
    p.add_argument("--pages", type=int, default=1,
                   help="每个关键词抓取的搜索结果页数（ChiCTR 每页 10 条）")
    p.add_argument("--detail-cap", type=int, default=30,
                   help="每个源详情页抓取上限（按去重后命中顺序）")
    p.add_argument("--db", default=None,
                   help="可选：重定向数据库路径（隔离案例库场景；"
                        "必须在导入采集器前完成，故在此解析）")
    p.add_argument("--output", default=None,
                   help="可选：统计 JSON 输出路径（默认仅打印 stdout）")
    return p.parse_args()


ARGS = parse_args()

# ── 隔离重定向（必须在导入 db/collectors 之前完成） ──────────────────────
import config  # noqa: E402

if ARGS.db:
    config.CONFIG.db.path = Path(ARGS.db)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("keyword_sweep")

KEYWORDS = [k.strip() for k in ARGS.keywords.split(",") if k.strip()]
if not KEYWORDS:
    raise SystemExit("至少需要一个检索词")


def sweep_chictr() -> dict:
    """ChiCTR 定向检索（HTTP 热路径）：搜索 → 详情 → upsert。"""
    from collectors.chictr import (ChiCTRCollector, CHICTR_BASE_URL,
                                   CHICTR_DETAIL_PATH)
    from core.waf_guard import WafCircuitOpenError

    out: dict = {"keyword_hits": {}, "unique_hits": 0,
                 "enriched": 0, "failed": 0, "stub": 0, "nos": []}
    try:
        chi = ChiCTRCollector()
        seen: dict[str, dict] = {}
        for kw in KEYWORDS:
            try:
                total = chi._search_total_count(kw)
                entries: list[dict] = []
                for page in range(1, ARGS.pages + 1):
                    entries += chi._search_results_page(kw, page)
            except Exception as exc:
                log.warning("ChiCTR %r 检索失败: %s", kw, exc)
                out["keyword_hits"][kw] = -1
                continue
            out["keyword_hits"][kw] = total
            for e in entries:
                if e.get("chictr_no") and e.get("proj_id"):
                    seen[e["chictr_no"]] = e
            log.info("ChiCTR %r: total=%s, 抓取 %d 条", kw, total, len(entries))

        out["unique_hits"] = len(seen)
        for no, e in list(seen.items())[:ARGS.detail_cap]:
            try:
                html = chi._fetch(
                    f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={e['proj_id']}")
                if chi._is_stub_page(html):
                    out["stub"] += 1
                    continue
                parsed = chi.parse_detail_page(html, no)
                raw = {"chictr_no": parsed.get("source_trial_id") or no,
                       "proj_id": e["proj_id"], "html": html,
                       "parsed_fields": parsed}
                chi._upsert_and_emit_events(chi.normalise(raw))
                out["enriched"] += 1
                out["nos"].append(parsed.get("source_trial_id") or no)
                log.info("ChiCTR 定向增强完成 %s", no)
            except WafCircuitOpenError:
                log.error("ChiCTR 熔断中止，剩余命中本轮不再抓取")
                break
            except Exception as exc:
                out["failed"] += 1
                log.warning("ChiCTR 定向增强失败 %s: %s", no, exc)
    except WafCircuitOpenError:
        out["circuit_open"] = True
        log.error("ChiCTR 熔断已打开")
    return out


def sweep_ctr() -> dict:
    """CTR 定向检索（Playwright 浏览器路径）：搜索 → 详情 → upsert。"""
    from collectors.chinadrugtrials import (ChinaDrugTrialsCollector,
                                            _is_stub_page)
    from core.waf_guard import WafCircuitOpenError

    out: dict = {"keyword_hits": {}, "unique_hits": 0,
                 "enriched": 0, "failed": 0, "stub": 0, "nos": []}
    try:
        ctr = ChinaDrugTrialsCollector()
        seen: dict[str, dict] = {}
        for kw in KEYWORDS:
            try:
                entries = ctr._search_keyword(kw, max_pages=ARGS.pages)
            except Exception as exc:
                log.warning("CTR %r 检索失败: %s", kw, exc)
                out["keyword_hits"][kw] = -1
                continue
            out["keyword_hits"][kw] = len(entries)
            for e in entries:
                if e.get("uuid"):
                    seen[e["uuid"]] = e
            log.info("CTR %r: page 命中 %d 条", kw, len(entries))

        out["unique_hits"] = len(seen)
        delay = ctr.cfg.request_delay_sec or 5.0
        for uuid, e in list(seen.items())[:ARGS.detail_cap]:
            ctr_no = e.get("ctr") or ""
            try:
                html = ctr.fetch_detail_page(uuid, e.get("index", "1"))
                if not html:
                    raise RuntimeError("详情页抓取失败（WAF/超时）")
                if _is_stub_page(html):
                    out["stub"] += 1
                    continue
                parsed = ctr.parse_detail_html(html, ctr_no)
                raw = {"ctr_number": parsed.get("source_trial_id") or ctr_no,
                       "uuid": uuid, "html": html,
                       "parsed_fields": parsed}
                ctr._upsert_and_emit_events(ctr.normalise(raw))
                out["enriched"] += 1
                out["nos"].append(parsed.get("source_trial_id") or ctr_no)
                log.info("CTR 定向增强完成 %s", ctr_no)
            except WafCircuitOpenError:
                log.error("CTR 熔断中止，剩余命中本轮不再抓取")
                break
            except Exception as exc:
                out["failed"] += 1
                log.warning("CTR 定向增强失败 %s: %s", ctr_no, exc)
            time.sleep(delay)
    except WafCircuitOpenError:
        out["circuit_open"] = True
        log.error("CTR 熔断已打开")
    return out


def main() -> None:
    from collectors.browser_base import close_shared_browser
    from db.schema import create_schema

    create_schema()
    t0 = time.time()
    stats: dict = {"keywords": KEYWORDS, "pages": ARGS.pages,
                   "detail_cap": ARGS.detail_cap}
    if ARGS.source in ("chictr", "all"):
        stats["chictr"] = sweep_chictr()
    if ARGS.source in ("ctr", "all"):
        stats["ctr"] = sweep_ctr()
    stats["seconds"] = round(time.time() - t0, 1)

    rendered = json.dumps(stats, ensure_ascii=False, indent=2)
    if ARGS.output:
        Path(ARGS.output).write_text(rendered, encoding="utf-8")
        log.info("统计已写入 %s", ARGS.output)
    close_shared_browser()
    print(rendered)


if __name__ == "__main__":
    main()
