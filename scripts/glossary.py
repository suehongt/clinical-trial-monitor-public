#!/usr/bin/env python3
"""Inspect and curate the learned bilingual glossary (glossary_terms, v19).

Mining runs inside the search API (core.terminology.learn_from_results):
every search whose query has no known translation tries to induce one from
same-record zh/en field pairs in the result set. Candidates with evidence
≥2 distinct records auto-activate; this CLI is the review surface.

Usage:
    python scripts/glossary.py list [--status candidate|active|retired]
    python scripts/glossary.py stats
    python scripts/glossary.py add <中文词> <english term>   # manual, active
    python scripts/glossary.py activate <term_id>
    python scripts/glossary.py retire <term_id>              # wrong pair → excluded
    python scripts/glossary.py remove <term_id>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _conn():
    from db.connection import get_connection
    from db.schema import create_schema
    create_schema()
    return get_connection()


def cmd_list(args) -> None:
    conn = _conn()
    sql = ("SELECT term_id, zh_term, en_term, source, status, evidence, hits, "
           "last_seen_at FROM glossary_terms")
    params: list = []
    if args.status:
        sql += " WHERE status = ?"
        params.append(args.status)
    sql += " ORDER BY status, evidence DESC, term_id"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("(empty)")
        return
    print(f"{'id':>5}  {'status':<9} {'ev':>3} {'hits':>4}  {'zh_term':<24} "
          f"{'en_term':<32} src")
    for r in rows:
        print(f"{r['term_id']:>5}  {r['status']:<9} {r['evidence']:>3} "
              f"{r['hits']:>4}  {r['zh_term']:<24} {r['en_term']:<32} "
              f"{r['source']} {r['last_seen_at'] or ''}")


def cmd_stats(args) -> None:
    conn = _conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM glossary_terms "
        "GROUP BY status ORDER BY status").fetchall()
    total = sum(r["n"] for r in rows)
    print(f"glossary_terms: {total} entries")
    for r in rows:
        print(f"  {r['status']:<10} {r['n']}")


def cmd_add(args) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO glossary_terms (zh_term, en_term, source, status, evidence)"
        " VALUES (?, ?, 'manual', 'active', 99)"
        " ON CONFLICT(zh_term, en_term) DO UPDATE SET"
        " status='active', source='manual'",
        (args.zh_term.strip(), args.en_term.strip().lower()))
    conn.commit()
    print(f"added: {args.zh_term} ↔ {args.en_term} (active)")


def _set_status(term_id: int, status: str) -> None:
    conn = _conn()
    cur = conn.execute("UPDATE glossary_terms SET status = ? WHERE term_id = ?",
                       (status, term_id))
    conn.commit()
    print(f"{status}: {cur.rowcount} row(s) updated" if cur.rowcount
          else f"no glossary_terms row with id={term_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list glossary entries")
    p_list.add_argument("--status", choices=["candidate", "active", "retired"])
    p_list.set_defaults(func=cmd_list)

    p_stats = sub.add_parser("stats", help="counts by status")
    p_stats.set_defaults(func=cmd_stats)

    p_add = sub.add_parser("add", help="manually add an active pair")
    p_add.add_argument("zh_term")
    p_add.add_argument("en_term")
    p_add.set_defaults(func=cmd_add)

    for name, status in (("activate", "active"), ("retire", "retired")):
        p = sub.add_parser(name, help=f"set status → {status}")
        p.add_argument("term_id", type=int)
        p.set_defaults(func=lambda a, s=status: _set_status(a.term_id, s))

    p_rm = sub.add_parser("remove", help="delete a row")
    p_rm.add_argument("term_id", type=int)
    p_rm.set_defaults(func=lambda a: print(
        "removed:", _conn().execute(
            "DELETE FROM glossary_terms WHERE term_id = ?",
            (a.term_id,)).rowcount, "row(s)") or _conn().commit())

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
