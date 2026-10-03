#!/usr/bin/env python3
"""Concise, reusable audit of Phase-1 trial-event severity classifications.

Usage: python scripts/audit_event_severity.py [path/to/ct_monitor.db]
The script intentionally selects field-level values only (never raw_payload).
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def compact(value: object, limit: int = 180) -> str:
    text = "" if value is None else " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def grouped(conn: sqlite3.Connection, label: str, select: str, group_by: str) -> None:
    print(f"\n{label}:")
    for row in conn.execute(f"SELECT {select}, COUNT(*) count FROM trial_events te "
                            f"JOIN registry_records r ON r.record_id=te.record_id "
                            f"LEFT JOIN registry_sources s ON s.source_id=r.source_id "
                            f"GROUP BY {group_by} ORDER BY count DESC"):
        print("  " + " | ".join(str(x) for x in row))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", nargs="?", default="db/ct_monitor.db")
    args = parser.parse_args()
    path = Path(args.database)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM trial_events").fetchone()[0]
    print(f"Severity audit: {path}\nTotal events: {total}")
    grouped(conn, "By severity", "COALESCE(te.severity, 'normal')", "COALESCE(te.severity, 'normal')")
    grouped(conn, "By field", "te.field_name", "te.field_name")
    grouped(conn, "By change type", "COALESCE(te.change_type, 'modified')", "COALESCE(te.change_type, 'modified')")
    grouped(conn, "By severity × field", "COALESCE(te.severity, 'normal'), te.field_name", "COALESCE(te.severity, 'normal'), te.field_name")
    grouped(conn, "By severity × registry", "COALESCE(te.severity, 'normal'), COALESCE(s.short_name, 'unknown')", "COALESCE(te.severity, 'normal'), COALESCE(s.short_name, 'unknown')")

    for severity in ("critical", "important", "normal", "minor"):
        rows = conn.execute("""SELECT r.source_trial_id, s.short_name registry, te.field_name,
                    te.old_value, te.new_value, te.change_type, te.severity,
                    te.importance_score, te.detected_at
             FROM trial_events te JOIN registry_records r ON r.record_id=te.record_id
             JOIN registry_sources s ON s.source_id=r.source_id
             WHERE te.severity=? ORDER BY te.detected_at DESC, te.event_id DESC LIMIT ?""",
                            (severity, 20 if severity == "critical" else 5)).fetchall()
        print(f"\n{severity.upper()} representative samples ({len(rows)} shown):")
        for row in rows:
            print(f"  {row['source_trial_id']} [{row['registry']}] {row['field_name']} "
                  f"({row['change_type']}, score={row['importance_score']}, {row['detected_at']})")
            print(f"    old: {compact(row['old_value'])}\n    new: {compact(row['new_value'])}")


if __name__ == "__main__":
    main()
