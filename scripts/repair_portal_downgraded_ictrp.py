#!/usr/bin/env python3
"""One-off repair: restore ICTRP records downgraded by portal live-search stubs.

``WhoIctrpCollector.ingest_live_entries`` used to upsert the search grid's
thin rows (title/status/date only — the grid does not expose recruitment
countries) over any latest record, including full XML-snapshot rows.  The
stub became ``is_latest`` and the snapshot row's countries, sponsors,
conditions, phase and enrollment disappeared from search/facets until the
next weekly snapshot re-versioned the trial.

The collector now skips hits whose latest record is already full; this
script repairs the existing damage:

1. for every latest portal-live stub that has a superseded full row, flip
   ``is_latest`` back to the newest full row **and swap the two rows'
   version numbers**, so the restored full row carries the highest version.
   Without the swap the version chain is inverted (``is_latest`` on a lower
   version) and the integrity audit's INVALID_LATEST_VERSION rule fails —
   which is how the 2026-10-01 nightly pipeline ended up "repairing" the
   audit errors by flipping the stubs right back;
2. delete the phantom "field cleared" trial_events the downgrade produced
   (old value non-empty, new value empty, on a portal-live stub row) —
   these are diff noise against fields the grid never carried, and they
   linger on stubs that were already record-repaired.

Usage:
    python scripts/repair_portal_downgraded_ictrp.py            # dry-run
    python scripts/repair_portal_downgraded_ictrp.py --apply    # write

Idempotent: re-running reports 0 pending restores / 0 phantom events.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection, close_connection

_STUBS = """
SELECT r.record_id AS stub_id, r.source_id, r.source_trial_id,
       r.version_number AS stub_version
FROM registry_records r
JOIN registry_sources s ON s.source_id = r.source_id
WHERE s.short_name = 'ICTRP' AND r.is_latest = 1
  AND json_valid(r.raw_payload)
  AND json_extract(r.raw_payload, '$.who_ictrp_record._portal_live') = 1
"""

_NEWEST_FULL = """
SELECT record_id, version_number AS full_version FROM registry_records
WHERE source_id = ? AND source_trial_id = ? AND is_latest = 0
  AND json_valid(raw_payload)
  AND COALESCE(json_extract(raw_payload,
        '$.who_ictrp_record._portal_live'), 0) = 0
ORDER BY record_id DESC LIMIT 1
"""

# "Field cleared" events on a portal-live stub row: the grid never carried
# these fields, so an empty new_value is data loss from downgrading, not a
# real registry change.
_PHANTOM_EVENT_COUNT = """
SELECT count(*) FROM trial_events te
JOIN registry_records r ON r.record_id = te.record_id
JOIN registry_sources s ON s.source_id = r.source_id
WHERE s.short_name = 'ICTRP'
  AND json_valid(r.raw_payload)
  AND json_extract(r.raw_payload, '$.who_ictrp_record._portal_live') = 1
  AND COALESCE(te.old_value, '') <> ''
  AND COALESCE(te.new_value, '') = ''
"""

_DELETE_PHANTOM_EVENTS = """
DELETE FROM trial_events WHERE event_id IN (
    SELECT te.event_id FROM trial_events te
    JOIN registry_records r ON r.record_id = te.record_id
    JOIN registry_sources s ON s.source_id = r.source_id
    WHERE s.short_name = 'ICTRP'
      AND json_valid(r.raw_payload)
      AND json_extract(r.raw_payload, '$.who_ictrp_record._portal_live') = 1
      AND COALESCE(te.old_value, '') <> ''
      AND COALESCE(te.new_value, '') = ''
)
"""


def find_downgraded(conn) -> list:
    """Return (stub_id, full_id, source_trial_id, stub_v, full_v) tuples
    pending restore."""
    pairs = []
    for stub in conn.execute(_STUBS).fetchall():
        full = conn.execute(_NEWEST_FULL,
                            (stub["source_id"], stub["source_trial_id"])
                            ).fetchone()
        if full is not None:
            pairs.append((stub["stub_id"], full["record_id"],
                          stub["source_trial_id"],
                          stub["stub_version"], full["full_version"]))
    return pairs


def repair(conn, apply: bool = False) -> int:
    """Restore superseded full rows over portal-live stubs and drop the
    phantom clear events.  Returns the number of records restored (0 in
    dry-run without --apply)."""
    pending = find_downgraded(conn)
    phantom = conn.execute(_PHANTOM_EVENT_COUNT).fetchone()[0]
    if not apply:
        print(f"Dry-run: {len(pending)} record(s) would be restored, "
              f"{phantom} phantom event(s) would be deleted. "
              f"Re-run with --apply to write.")
        return 0
    for stub_id, full_id, trial_id, stub_v, full_v in pending:
        conn.execute(
            "UPDATE registry_records SET is_latest = 0, superseded_by = ? "
            "WHERE record_id = ?", (full_id, stub_id))
        conn.execute(
            "UPDATE registry_records SET is_latest = 1, superseded_by = NULL "
            "WHERE record_id = ?", (full_id,))
        if full_v < stub_v:
            # Swap version numbers so the restored full row is the max
            # version — otherwise INVALID_LATEST_VERSION audit errors invite
            # the next integrity-driven "repair" to flip the stub back.
            conn.execute(
                "UPDATE registry_records SET version_number = ? "
                "WHERE record_id = ?", (-stub_v - 1, full_id))   # park
            conn.execute(
                "UPDATE registry_records SET version_number = ? "
                "WHERE record_id = ?", (full_v, stub_id))
            conn.execute(
                "UPDATE registry_records SET version_number = ? "
                "WHERE record_id = ?", (stub_v, full_id))
        print(f"  restored {trial_id}: record {full_id} (v{stub_v}) "
              f"over stub {stub_id} (was v{full_v})")
    conn.execute(_DELETE_PHANTOM_EVENTS)
    deleted = conn.execute(_PHANTOM_EVENT_COUNT).fetchone()[0]
    conn.commit()
    print(f"Applied {len(pending)} restore(s); deleted "
          f"{phantom - deleted} phantom event(s).")
    return len(pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes (default: dry-run)")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if not conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name='registry_records'"
        ).fetchone()[0]:
            print("Database not initialised — run `python run_monitor.py init` first.")
            sys.exit(1)
        repair(conn, apply=args.apply)
    finally:
        close_connection()


if __name__ == "__main__":
    main()
