#!/usr/bin/env python3
"""One-off migration: unify list-type fields to JSON string arrays.

Historically collectors stored conditions/interventions/sponsors/locations/
secondary_endpoints inconsistently (NCT: JSON arrays; ChiCTR/CTR/ICTRP:
plain or semicolon-joined text).  Collectors now always write the canonical
JSON array format; this script rewrites existing rows to match.

Rewrite rule (lossless):
  - value already a JSON array  -> unchanged
  - value containing newlines   -> one array item per non-empty line
  - any other non-empty value   -> single-element array

Usage:
    python scripts/migrate_json_fields.py            # dry-run (default)
    python scripts/migrate_json_fields.py --apply    # write changes

Idempotent: re-running reports 0 pending changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection, close_connection

FIELDS = ("conditions", "interventions", "sponsors", "locations", "secondary_endpoints")


def wrap_value(raw: str) -> str | None:
    """Return the canonical JSON array for a legacy value, or None if it is
    already canonical (valid JSON array) / empty."""
    s = raw.strip()
    if not s:
        return None
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return None  # already canonical
        except json.JSONDecodeError:
            pass
    items = [p.strip() for p in s.split("\n")] if "\n" in s else [s]
    items = [i for i in items if i]
    return json.dumps(items, ensure_ascii=False) if items else None


def migrate(conn, apply: bool = False) -> Dict[str, int]:
    """Scan and (optionally) rewrite legacy values.  Returns per-field counts."""
    cur = conn.execute(
        f"SELECT record_id, short_name, {', '.join(FIELDS)} "
        f"FROM registry_records r JOIN registry_sources s ON s.source_id = r.source_id"
    )
    rows = cur.fetchall()

    updates: list[tuple[int, str, str]] = []
    per_field: Dict[str, int] = {f: 0 for f in FIELDS}
    per_source: Dict[str, int] = {}

    for row in rows:
        for field in FIELDS:
            raw = row[field]
            if raw is None:
                continue
            new_val = wrap_value(raw)
            if new_val is None:
                continue
            updates.append((row["record_id"], field, new_val))
            per_field[field] += 1
            per_source[row["short_name"]] = per_source.get(row["short_name"], 0) + 1

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] scanned {len(rows)} records")
    for field in FIELDS:
        print(f"  {field:22s} {per_field[field]:6d} value(s) to rewrite")
    for src, n in sorted(per_source.items()):
        print(f"  source {src:8s} {n:6d} value(s)")

    if not updates:
        print("Nothing to migrate (all values already canonical JSON arrays).")
        return per_field

    if apply:
        print("\n⚠  Make sure db/ct_monitor.db is backed up before applying!")
        with conn:
            for record_id, field, new_val in updates:
                conn.execute(
                    f"UPDATE registry_records SET {field} = ? WHERE record_id = ?",
                    (new_val, record_id),
                )
        conn.commit()
        print(f"Applied {len(updates)} update(s).")
    else:
        print(f"\nDry-run: {len(updates)} value(s) would be rewritten. "
              f"Re-run with --apply to write.")
    return per_field


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
        migrate(conn, apply=args.apply)
    finally:
        close_connection()


if __name__ == "__main__":
    main()
