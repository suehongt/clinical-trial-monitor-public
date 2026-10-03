#!/usr/bin/env python3
"""Rebuild the records_fts full-text index from registry_records.

Normally the AFTER INSERT trigger keeps records_fts in sync and the v6
schema migration backfills once.  Run this only if the index ever drifts
(e.g. bulk edits were made outside the collector pipeline).

Usage:
    python scripts/rebuild_fts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection, close_connection
from db.schema import create_schema, fts_available


def main() -> None:
    create_schema()  # ensure records_fts exists on older databases

    conn = get_connection()
    if not fts_available():
        print("records_fts is not available (FTS5 missing in this SQLite "
              "build, or the database was not initialised).")
        sys.exit(1)

    before = conn.execute("SELECT count(*) FROM records_fts").fetchone()[0]
    conn.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
    conn.commit()
    after = conn.execute("SELECT count(*) FROM records_fts").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM registry_records").fetchone()[0]

    print(f"records_fts rebuilt: {before} -> {after} indexed rows "
          f"(registry_records: {total})")
    if after != total:
        print(f"⚠  index row count ({after}) != registry_records ({total}) — "
              f"some rows may have NULL/missing indexed columns")
    close_connection()


if __name__ == "__main__":
    main()
