#!/usr/bin/env python3
"""
Cross-source Data Quality Report.

Usage::

    python scripts/quality_report.py
    python scripts/quality_report.py --json   # JSON output
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.quality_checks import (get_quality_summary, field_completeness_stats,
                                 payload_storage_stats, discovery_funnel_stats)


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def main():
    parser = argparse.ArgumentParser(description="Cross-source Data Quality Report")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    summary = get_quality_summary()
    completeness = field_completeness_stats()
    payload = payload_storage_stats()

    if args.json:
        output = {"quality_summary": summary, "field_completeness": completeness,
                  "payload_storage": payload}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print("=" * 70)
    print("  Cross-Source Data Quality Report")
    print("=" * 70)

    # ── Registry records ──────────────────────────────────────────────
    print("\n📋 Registry Records Per Source:")
    for src, cnt in summary["records_per_source"].items():
        print(f"  {src:8s}  {cnt:>5} records")
    print(f"\n  Total master trials: {summary['total_master_trials']}")

    # ── Match status ──────────────────────────────────────────────────
    print("\n📎 Match Status Distribution:")
    for status, cnt in summary["match_status_counts"].items():
        print(f"  {status:20s}  {cnt}")

    # ── WHO ICTRP ─────────────────────────────────────────────────────
    print(f"\n🌐 WHO ICTRP records: {summary['who_ictrp_records']}")

    # ── NULL FKs ──────────────────────────────────────────────────────
    print("\n⚠️  NULL Foreign Keys:")
    print(f"  status_id:      {summary['null_status_id']}")
    print(f"  study_type_id:  {summary['null_study_type_id']}")

    # ── Field completeness ────────────────────────────────────────────
    print("\n📊 Field Completeness (% non-null per source):")
    header = f"{'Field':25s}"
    for row in completeness:
        header += f"  {row['source']:8s}"
    print(header)
    print("-" * len(header))
    for field in ["title", "enrollment", "conditions", "interventions",
                   "sponsors", "study_phase", "primary_endpoint"]:
        line = f"{field:25s}"
        for row in completeness:
            line += f"  {row.get(f'{field}_pct', 0):>7.1f}%"
        print(line)

    # ── Source failure rates ──────────────────────────────────────────
    print("\n📡 Source Failure Rates:")
    for src, info in summary["source_failure_rates"].items():
        print(f"  {src:8s}  {info['failures']}/{info['total_runs']} "
              f"runs failed ({_fmt_pct(info['failure_rate_pct'])})")

    # ── Issues ────────────────────────────────────────────────────────
    print("\n🔍 Detected Issues:")
    print(f"  False merge candidates:   {summary['false_merge_candidates']}")
    if summary["false_merge_details"]:
        for d in summary["false_merge_details"][:5]:
            print(f"    - [{d['severity']}] {d['issue']}: {d['detail'][:100]}")

    print(f"\n  Missed merge candidates:  {summary['missed_merge_candidates']}")
    if summary["missed_merge_details"]:
        for d in summary["missed_merge_details"][:5]:
            print(f"    - [{d.get('issue', 'title_similarity')}] {d['detail'][:120]}")

    print(f"\n  Duplicate records:        {summary['duplicate_records']}")
    if summary["duplicate_details"]:
        for d in summary["duplicate_details"][:5]:
            print(f"    - {d['source']}:{d['source_trial_id']} "
                  f"({d['duplicate_count']} copies)")

    print(f"\n  Multi-master records:     {summary['multi_master_records']}")
    if summary["multi_master_details"]:
        for d in summary["multi_master_details"][:5]:
            print(f"    - {d['source']}:{d['source_trial_id']} → "
                  f"{d['master_count']} masters")

    print(f"\n  Registry ID → Multi-master: {summary['registry_id_multi_master']}")
    if summary["registry_id_multi_master_details"]:
        for d in summary["registry_id_multi_master_details"][:5]:
            print(f"    - {d['identifier_type']}:{d['identifier_value']} → "
                  f"{d['master_count']} masters")

    print(f"\n  Unlinked records:         {summary['unlinked_records']}")

    # ── Payload storage ────────────────────────────────────────────────
    print("\n💾 Raw Payload Storage (registry_records.raw_payload):")
    print(f"  total raw_payload bytes:  {payload['raw_payload_bytes']:,} "
          f"across {payload['rows_total']:,} rows")
    for src, info in payload["raw_payload_per_source"].items():
        print(f"    {src:8s}  {info['payload_bytes']:>14,} bytes  ({info['rows']:,} rows)")
    ratio = payload.get("payload_vs_parsed_sample_ratio")
    if ratio is not None:
        print(f"  payload vs parsed-column sample ratio: {ratio}x "
              f"(>1 means raw payloads dominate; consider zlib BLOB storage)")

    # ── Discovery funnel（§2.6 发现层漏斗：转化率 + 各源解析产出） ──────
    funnel = discovery_funnel_stats()
    print("\n🔎 Discovery Funnel (discovery_queue 发现→增强转化):")
    totals = funnel["totals"]
    if not totals:
        print("  (empty — no discovery_queue rows)")
    else:
        print(f"  pending={totals.get('pending', 0)}  "
              f"enriched={totals.get('enriched', 0)}  "
              f"failed={totals.get('failed', 0)}  "
              f"skipped={totals.get('skipped', 0)}")
        rate = funnel["conversion_rate"]
        rate_s = f"{rate * 100:.1f}%" if rate is not None else "N/A"
        print(f"  conversion rate enriched/(enriched+failed+skipped): {rate_s}")
        for src, states in sorted(funnel["by_source_state"].items()):
            parts = ", ".join(f"{state}={cnt}"
                              for state, cnt in sorted(states.items()))
            print(f"    {src:8s}  {parts}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
