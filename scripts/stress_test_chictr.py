#!/usr/bin/env python3
"""WAF rate-ladder stress test for ChiCTR (read-only probing, batch-capped).

Climbs request pacing stepwise (2.0s -> 1.5s -> 1.0s), enriching pending
queue rows at each rung, and measures per-rung challenge/error rates.
Stops climbing as soon as a rung shows WAF challenges above the threshold
or two consecutive failures.  Safety: total requests capped by --per-rung;
the collectors' own 3-strike circuit breaker stays armed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))



def run_rung(pacing: float, budget: int) -> dict:
    """Enrich `budget` pending rows at the given pacing; return metrics."""
    from collectors.chictr import ChiCTRCollector

    collector = ChiCTRCollector()
    collector.cfg.extra["enrich_batch_size"] = budget
    collector.cfg.request_delay_sec = pacing

    started = time.time()
    stats = collector.enrich_pending(limit=budget)
    elapsed = time.time() - started

    n = max(1, stats.get("enriched", 0) + stats.get("failed", 0) + stats.get("skipped", 0))
    return {
        "pacing": pacing,
        "budget": budget,
        "enriched": stats.get("enriched", 0),
        "failed": stats.get("failed", 0),
        "skipped": stats.get("skipped", 0),
        "elapsed_s": round(elapsed, 1),
        "s_per_item": round(elapsed / n, 2),
    }


def pending_remaining() -> int:
    from db.connection import get_connection
    conn = get_connection()
    return conn.execute(
        "SELECT count(*) FROM discovery_queue WHERE state='pending' "
        "AND source_id=(SELECT source_id FROM registry_sources WHERE short_name='ChiCTR')"
    ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-rung", type=int, default=60,
                        help="Requests per rung (default: 60)")
    parser.add_argument("--rungs", default="2.0,1.5,1.0",
                        help="Comma-separated pacing seconds per rung")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    os.environ["CT_DISEASE_PROFILE"] = "mi"

    rungs = [float(x) for x in args.rungs.split(",")]
    total_pending = pending_remaining()
    print(f"stress test start: pending={total_pending}, rungs={rungs}, "
          f"per-rung={args.per_rung}", flush=True)

    results = []
    for i, pacing in enumerate(rungs, 1):
        remaining = pending_remaining()
        if remaining == 0:
            print(f"[rung {i}] queue drained — nothing left to test", flush=True)
            break
        budget = min(args.per_rung, remaining)
        print(f"[rung {i}] pacing={pacing}s budget={budget}", flush=True)

        # collect challenge warnings emitted during this rung
        try:
            m = run_rung(pacing, budget)
        except Exception as exc:
            print(f"[rung {i}] ABORTED: {exc}", flush=True)
            results.append({"pacing": pacing, "aborted": str(exc)[:120]})
            break
        m["rung"] = i
        results.append(m)
        print(f"[rung {i}] result: {m}", flush=True)

        # decision: climb or stop
        if m.get("failed", 0) >= 2 or m.get("skipped", 0) > budget * 0.3:
            print(f"[rung {i}] WAF pressure detected (failed={m['failed']} "
                  f"skipped={m['skipped']}) — NOT climbing further", flush=True)
            break

    print("\n=== ladder summary ===", flush=True)
    for r in results:
        print(json_line(r), flush=True)


def json_line(r: dict) -> str:
    import json
    return json.dumps(r, ensure_ascii=False)


if __name__ == "__main__":
    main()
