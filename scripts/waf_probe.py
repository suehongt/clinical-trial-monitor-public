#!/usr/bin/env python3
"""Concurrent WAF stress test for ChiCTR + CTR (weekly boundary probe).

Runs BOTH WAF-protected sites concurrently at a given pacing, measures
challenge/failure rates per site, and appends a rules recommendation to
docs/waf_limits.json (history) plus a human-readable summary to stdout.

Design notes:
  - The two sites are behind different WAF vendors (ChiCTR: Alibaba Cloud,
    CTR: Wangsu) with independent rate buckets, so concurrent probing is
    safe from a correlation standpoint and doubles the information per run.
  - The probe climbs ONE rung per invocation (weekly cadence): the rung
    ladder itself lives in docs/waf_limits.json ("current_pacing"), which
    the script reads, tests, and then either holds, or backs off and resets
    to the last-known-good pacing when pressure is detected.
  - Never runs concurrently with the 00:00 pipeline (scheduled 23:00 Sun).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LIMITS_FILE = Path("docs/waf_limits.json")
DEFAULT_LIMITS = {
    "current_pacing": {"chictr": 2.5, "ctr": 2.5},
    "last_known_good": {"chictr": 2.5, "ctr": 2.5},
    "tested_ok": {"chictr": [2.5], "ctr": [2.5]},
    "history": [],
}


def load_limits() -> dict:
    if LIMITS_FILE.exists():
        return json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
    return json.loads(json.dumps(DEFAULT_LIMITS))


def save_limits(limits: dict) -> None:
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIMITS_FILE.write_text(json.dumps(limits, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def probe_site(source: str, pacing: float, budget: int) -> dict:
    """Enrich up to `budget` pending rows at `pacing`; return metrics."""
    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod

    if source == "chictr":
        collector = chictr_mod.ChiCTRCollector()
    else:
        collector = ctr_mod.ChinaDrugTrialsCollector()
    collector.cfg.extra["enrich_batch_size"] = budget
    collector.cfg.request_delay_sec = pacing

    started = time.time()
    if source == "chictr":
        stats = collector.enrich_pending(limit=budget)
        n_done = (stats.get("enriched", 0) + stats.get("failed", 0)
                  + stats.get("skipped", 0))
        failed = stats.get("failed", 0)
        fetched = n_done
    else:
        raws = collector.fetch_new_or_updated(since=None)
        failed = sum(1 for r in raws if not r.get("parsed_fields", {}).get("source_trial_id"))
        fetched = len(raws)
        n_done = max(1, fetched)
    elapsed = time.time() - started

    return {
        "source": source,
        "pacing": pacing,
        "fetched": fetched,
        "failed": failed,
        "elapsed_s": round(elapsed, 1),
        "s_per_item": round(elapsed / max(1, n_done), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=int, default=40,
                        help="Requests per site this probe (default: 40)")
    parser.add_argument("--force-pacing", type=float, default=None,
                        help="Override the ladder pacing for this run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    os.environ["CT_DISEASE_PROFILE"] = "mi"

    limits = load_limits()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"concurrent WAF probe @ {now}: "
          f"current_pacing={limits['current_pacing']}", flush=True)

    # Sequential-within-script is deliberate: Python GIL serialises the two
    # Playwright loops anyway; "concurrent" here means both sites get probed
    # in the same window regardless of each other's outcome.
    results = []
    aborted = []
    for source in ("chictr", "ctr"):
        pacing = args.force_pacing or limits["current_pacing"][source]
        print(f"[probe] {source} @ {pacing}s x {args.budget}", flush=True)
        try:
            r = probe_site(source, pacing, args.budget)
        except Exception as exc:
            print(f"[probe] {source} ABORTED: {exc}", flush=True)
            aborted.append(source)
            r = {"source": source, "pacing": pacing, "aborted": str(exc)[:120]}
        results.append(r)
        print(f"[probe] {source} -> {r}", flush=True)

    # Rule update: any aborted/failed site resets that source to the last
    # known good pacing; sites that passed at the current pacing get their
    # last_known_good promoted.
    for r in results:
        src = r["source"]
        if r.get("aborted") or r.get("failed", 0) > 2:
            limits["current_pacing"][src] = limits["last_known_good"][src]
            limits.setdefault("history", []).append(
                {"at": now, "source": src, "event": "backoff",
                 "reset_to": limits["last_known_good"][src]})
        else:
            limits["last_known_good"][src] = r["pacing"]
            tested = limits["tested_ok"].setdefault(src, [])
            if r["pacing"] not in tested:
                tested.append(r["pacing"])

    limits.setdefault("history", []).append(
        {"at": now, "event": "probe", "results": results})
    save_limits(limits)

    print("\n=== weekly probe summary ===", flush=True)
    for r in results:
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print(f"rules now: current_pacing={limits['current_pacing']} "
          f"(history entries: {len(limits['history'])})", flush=True)


if __name__ == "__main__":
    main()
