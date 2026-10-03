#!/usr/bin/env python3
"""One WAF-safe crawl batch for ChiCTR / CTR (run daily, advances coverage).

Implements the agreed crawl rules:
  - batch size cap (default 40 new detail pages per run per source)
  - in-batch pacing from config (2.5s between requests)
  - already-stored trials are skipped, so consecutive daily batches advance
    coverage instead of refetching
  - WAF circuit breaker: the collectors abort a batch after 3 consecutive
    fetch failures

Usage:
    python scripts/crawl_waf_batch.py --source chictr
    python scripts/crawl_waf_batch.py --source ctr
    python scripts/crawl_waf_batch.py --source ctr --profiles cmp --batch 40
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cjk_keywords(profiles: list[str]) -> list[str]:
    """Chinese search keywords from the given disease profiles."""
    from config import DISEASE_PROFILES
    kws: list[str] = []
    for p in profiles:
        for kw in DISEASE_PROFILES[p]["report_keywords"]:
            if re.search(r"[\u4e00-\u9fff]", kw) and kw not in kws:
                kws.append(kw)
    return kws


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", choices=["chictr", "ctr"], required=True)
    parser.add_argument("--profiles", default="cmp,mi,hf,mm",
                        help="Comma-separated profile keys whose Chinese "
                             "keywords drive the search (default: all)")
    parser.add_argument("--batch", type=int, default=40,
                        help="Max NEW detail pages this batch (default: 40)")
    parser.add_argument("--full", action="store_true",
                        help="Full coverage: discover + enrich the entire pending "
                             "queue in one run (overrides --batch)")
    parser.add_argument("--workers", type=int,
                        default=int(__import__("os").environ.get(
                            "CT_WAF_WORKERS", "2")),
                        help="Parallel enrich sessions (cookie pool; each "
                             "keeps full pacing — per-session rate is "
                             "unchanged, default 2, env CT_WAF_WORKERS)")
    parser.add_argument("--incremental", action="store_true",
                        help="Small incremental: watermark discovery (stops as soon "
                             "as the cursor is reached) + enrich up to --batch — "
                             "for frequent low-volume runs spaced hours apart")
    args = parser.parse_args()

    keywords = cjk_keywords([k.strip() for k in args.profiles.split(",")])
    if not keywords:
        print("no Chinese keywords found for profiles", args.profiles)
        sys.exit(1)

    # 单实例护栏：同一站点同时只允许一个爬取进程（flock 持有至退出）。
    # 防止 --full 排干与定时增量/0 点批爬并发叠加同一 WAF 的请求压力；
    # 拿不到锁以退出码 0 优雅退出，调度方（自动化简报）按跳过处理。
    import fcntl
    lock_path = Path(os.environ.get("TMPDIR", "/tmp")) / (
        "ct_monitor_crawl_" + args.source + ".lock")
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another crawl_waf_batch for {args.source} is already "
              "running (lock held) — skipping this run", flush=True)
        return

    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    os.environ["CT_DISEASE_PROFILE"] = "mi"  # any valid key; keywords passed explicitly

    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    chictr_mod.BOOTSTRAP_KEYWORDS = keywords
    ctr_mod.BOOTSTRAP_KEYWORDS = keywords

    if args.source == "chictr":
        from collectors.chictr import ChiCTRCollector
        collector = ChiCTRCollector()
    else:
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        collector = ChinaDrugTrialsCollector()
    collector.cfg.max_records_per_run = args.batch

    print(f"batch start: source={args.source} keywords={keywords} cap={args.batch}",
          flush=True)

    if args.full:
        # Full-coverage mode: refresh the discovery queue, then enrich
        # EVERYTHING pending in one run (the daily batch caps at
        # enrich_batch_size=50/night; --full drains the whole queue).
        stats = collector.discover_new()
        print(f"discover: {stats}", flush=True)
        pending = collector.enrich_pending(limit=10**9, keywords=keywords,
                                          workers=args.workers)
        print(f"full done: { {k: v for k, v in pending.items() if k != 'records'} }",
              flush=True)
        return

    if args.incremental:
        # 小增量：水位线发现（命中水位即停，通常每词 1-2 页）+ 预算内
        # enrich。供低频轮询调度使用（间隔 ≥ 数小时，见 the project WAF notes WAF 红线）。
        stats = collector.discover_new()
        print(f"discover: {stats}", flush=True)
        enrich = collector.enrich_pending(limit=args.batch, keywords=keywords,
                                         workers=args.workers)
        print(f"incremental done: { {k: v for k, v in enrich.items() if k != 'records'} }",
              flush=True)
        return

    raws = collector.fetch_new_or_updated(since=None)

    stored_new = stored_updated = errors = 0
    for raw in raws:
        try:
            result = collector._upsert_record(collector.normalise(raw))
            if result["action"] == "new":
                stored_new += 1
            elif result["action"] == "updated":
                stored_updated += 1
        except Exception as exc:
            errors += 1
            print(f"  upsert error: {exc}", flush=True)

    print(f"batch done: fetched={len(raws)} new={stored_new} "
          f"updated={stored_updated} errors={errors}", flush=True)


if __name__ == "__main__":
    main()
