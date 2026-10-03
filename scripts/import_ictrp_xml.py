"""Bulk-import a WHO ICTRP XML snapshot into the DB (no request delay).

Usage:
    python scripts/import_ictrp_xml.py [--xml data/ictrp_mm_export.xml]

The XML export is produced by scripts/fetch_ictrp_xml.py (or downloaded
manually from trialsearch.who.int).  Records are marked is_bootstrap=1
(AGGREGATOR source — never counted as independent discoveries).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors.who_ictrp import WHOICTRPCollector
from config import ACTIVE_PROFILE_KEY


def import_snapshot(xml_path, records=None, collector=None) -> dict:
    """Import one ICTRP XML snapshot into the DB (bulk upsert, no delay).

    Reusable entry point for scripts/ictrp_weekly.py; the CLI ``main``
    below is a thin wrapper with identical behaviour to the original.

    Args:
        xml_path: path to the XML export file (must exist).
        records: optional pre-parsed record list — skips the file read +
            parse when the caller already parsed the same snapshot
            (e.g. for the diff step).
        collector: optional WHOICTRPCollector instance (one is created
            when omitted).

    Returns:
        Summary dict with keys ``parsed/new/updated/skipped/errors`` and
        ``records`` (the parsed raw dicts, for downstream diffing).
    """
    if records is None:
        xml_content = xml_path.read_text(encoding="utf-8")
        records = WHOICTRPCollector.parse_xml(xml_content)
    print(f"Parsed {len(records)} records from {xml_path}")

    collector = collector or WHOICTRPCollector()
    summary = {"parsed": len(records), "new": 0, "updated": 0,
               "skipped": 0, "errors": 0, "records": records}

    for i, raw in enumerate(records):
        try:
            norm = collector.normalise(raw)
            collector._save_raw_json(norm.source_trial_id, raw)
            result = collector._upsert_record(norm, is_bootstrap=True)
            summary[result["action"]] += 1
        except Exception as e:
            summary["errors"] += 1
            if summary["errors"] <= 5:
                print(f"  Error [{i}]: {e}")

        if (i + 1) % 500 == 0:
            print(f"  Progress: {i+1}/{len(records)} — "
                  f"new={summary['new']} upd={summary['updated']} "
                  f"skipped={summary['skipped']} err={summary['errors']}")

    # Finalise sync status
    from db.connection import get_connection
    conn = get_connection()
    source_id = collector.source_id
    cur = conn.execute(
        "SELECT count(*) FROM registry_records WHERE source_id = ?", (source_id,)
    )
    actual_count = cur.fetchone()[0]

    conn.execute(
        "UPDATE sync_status SET last_successful_sync = datetime('now'), "
        "updated_at = datetime('now'), last_record_count = ?, "
        "bootstrap_completed = 1 WHERE source_id = ?",
        (actual_count, source_id),
    )
    conn.commit()

    print(f"\nDone. {len(records)} processed.")
    print(f"  New: {summary['new']}, Updated: {summary['updated']}, "
          f"Skipped: {summary['skipped']}, Errors: {summary['errors']}")
    print(f"  DB record count for ICTRP: {actual_count}")
    return summary


def main():
    default_xml = f"data/ictrp_{ACTIVE_PROFILE_KEY}_export.xml"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xml", default=default_xml,
                        help=f"ICTRP XML export path (default: {default_xml}, "
                             "matches the active CT_DISEASE_PROFILE)")
    args = parser.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"XML not found: {xml_path} — run scripts/fetch_ictrp_xml.py first.")
        sys.exit(1)

    import_snapshot(xml_path)


if __name__ == "__main__":
    main()
