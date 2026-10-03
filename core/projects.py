"""Read-only project projections and composite scope SQL.

Projects are organizational references. No search, matching, watch, or
notification behavior lives here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from config import CONFIG
from core.ingestion import registry_health


def project_row(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM research_projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise LookupError("project not found")
    return dict(row)


def project_event_clause(project_id: int, *, record: str = "r", source: str = "s",
                         event: str = "te") -> tuple[str, list[int]]:
    """One row per trial_event; never fan out over matching project assets."""
    trial = f"{record}.source_trial_id"
    curated = (f"EXISTS (SELECT 1 FROM project_trials pt WHERE pt.project_id=? "
               f"AND pt.trial_id={trial} AND pt.source={source}.short_name)")
    monitors = (f"EXISTS (SELECT 1 FROM project_monitors pm "
                f"JOIN monitor_events me ON me.monitor_id=pm.monitor_id "
                f"WHERE pm.project_id=? AND (me.related_change_event_id={event}.event_id "
                f"OR me.trial_id={trial}))")
    members = (f"EXISTS (SELECT 1 FROM project_monitors pm "
               f"JOIN monitor_trials mt ON mt.monitor_id=pm.monitor_id "
               f"WHERE pm.project_id=? AND mt.trial_id={trial} AND mt.currently_matches=1)")
    return f"({curated} OR {monitors} OR {members})", [project_id] * 3


def project_current_clause(project_id: int, *, record: str = "r", source: str = "s") -> tuple[str, list[int]]:
    trial = f"{record}.source_trial_id"
    return (f"(EXISTS (SELECT 1 FROM project_trials pt WHERE pt.project_id=? "
            f"AND pt.trial_id={trial} AND pt.source={source}.short_name) "
            f"OR EXISTS (SELECT 1 FROM project_monitors pm JOIN monitor_trials mt "
            f"ON mt.monitor_id=pm.monitor_id WHERE pm.project_id=? "
            f"AND mt.trial_id={trial} AND mt.currently_matches=1))", [project_id, project_id])


def detail(conn: sqlite3.Connection, project_id: int, *, trial_page: int = 1,
           page_size: int = 100, watched_only: bool = False,
           unbounded: bool = False) -> dict[str, Any]:
    project = project_row(conn, project_id)
    searches = [dict(r) for r in conn.execute("""SELECT ss.*, pss.added_at FROM project_saved_searches pss
        JOIN saved_searches ss ON ss.id=pss.saved_search_id WHERE pss.project_id=? ORDER BY ss.name""", (project_id,))]
    monitors = [dict(r) for r in conn.execute("""SELECT m.*, pm.added_at,
        (SELECT COUNT(*) FROM monitor_trials mt WHERE mt.monitor_id=m.id AND mt.currently_matches=1) current_trials
        FROM project_monitors pm JOIN monitors m ON m.id=pm.monitor_id
        WHERE pm.project_id=? ORDER BY m.name""", (project_id,))]
    trial_total = conn.execute("SELECT COUNT(*) FROM project_trials WHERE project_id=?", (project_id,)).fetchone()[0]
    watched_total = conn.execute("""SELECT COUNT(*) FROM project_trials pt WHERE pt.project_id=? AND
        EXISTS(SELECT 1 FROM trial_watches w WHERE w.trial_id=pt.trial_id AND w.enabled=1)""", (project_id,)).fetchone()[0]
    trials = [dict(r) for r in conn.execute("""SELECT pt.source, pt.trial_id, pt.added_at, r.title,
        st.label status, r.study_phase phase,
        (SELECT max(te.detected_at) FROM trial_events te JOIN registry_records rr ON rr.record_id=te.record_id
         JOIN registry_sources ss ON ss.source_id=rr.source_id WHERE rr.source_trial_id=pt.trial_id AND ss.short_name=pt.source) last_change,
        (SELECT te.severity FROM trial_events te JOIN registry_records rr ON rr.record_id=te.record_id
         JOIN registry_sources ss ON ss.source_id=rr.source_id WHERE rr.source_trial_id=pt.trial_id AND ss.short_name=pt.source
         AND te.detected_at>=datetime('now','-7 days')
         ORDER BY CASE te.severity WHEN 'critical' THEN 4 WHEN 'important' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END DESC LIMIT 1) recent_severity,
        EXISTS(SELECT 1 FROM trial_watches w WHERE w.trial_id=pt.trial_id AND w.enabled=1) watching
        FROM project_trials pt LEFT JOIN registry_records r ON r.record_id=(
            SELECT r2.record_id FROM registry_records r2 JOIN registry_sources s2 ON s2.source_id=r2.source_id
            WHERE r2.source_trial_id=pt.trial_id AND s2.short_name=pt.source AND r2.is_latest=1 LIMIT 1)
        LEFT JOIN status_types st ON st.status_type_id=r.status_id
        WHERE pt.project_id=? AND (?=0 OR EXISTS(SELECT 1 FROM trial_watches w
            WHERE w.trial_id=pt.trial_id AND w.enabled=1))
        ORDER BY pt.added_at DESC, pt.source, pt.trial_id
        LIMIT ? OFFSET ?""", (project_id, int(watched_only), -1 if unbounded else page_size,
                                   0 if unbounded else (trial_page - 1) * page_size))]
    notes = [dict(r) for r in conn.execute("SELECT * FROM project_notes WHERE project_id=? ORDER BY updated_at DESC,id DESC", (project_id,))]
    evidence = [dict(r) for r in conn.execute("""SELECT pe.*, te.field_name,te.old_value,te.new_value,
        te.severity,te.detected_at,r.source_trial_id trial_id,r.title,s.short_name source
        FROM project_evidence pe JOIN trial_events te ON te.event_id=pe.event_id
        JOIN registry_records r ON r.record_id=te.record_id JOIN registry_sources s ON s.source_id=r.source_id
        WHERE pe.project_id=? ORDER BY pe.created_at DESC,pe.id DESC""", (project_id,))]
    return {**project, "saved_searches": searches, "monitors": monitors, "trials": trials,
            "notes": notes, "evidence": evidence,
            "trial_page": trial_page, "trial_page_size": page_size, "watched_trial_count": watched_total,
            "counts": {"saved_searches": len(searches), "monitors": len(monitors), "trials": trial_total,
                       "notes": len(notes), "evidence": len(evidence)}}


def list_summaries(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict[str, Any]]:
    """Small list cards: grouped counts and one deduplicated event aggregate."""
    where = "" if include_archived else "WHERE archived_at IS NULL"
    projects = [dict(r) for r in conn.execute(f"SELECT * FROM research_projects {where} ORDER BY pinned DESC,updated_at DESC,id DESC")]
    if not projects:
        return []
    summaries = {p["id"]: p for p in projects}
    for p in projects:
        p["counts"] = {"saved_searches": 0, "monitors": 0, "trials": 0, "notes": 0, "evidence": 0}
        p.update(active_monitors=0, recent_important_changes=0, recent_critical_changes=0,
                 last_activity=None, freshness={"sources": [], "affected": []})
    for table, key in (("project_saved_searches", "saved_searches"), ("project_monitors", "monitors"),
                       ("project_trials", "trials"), ("project_notes", "notes"), ("project_evidence", "evidence")):
        for row in conn.execute(f"SELECT project_id,COUNT(*) n FROM {table} GROUP BY project_id"):
            if row["project_id"] in summaries:
                summaries[row["project_id"]]["counts"][key] = row["n"]
    for row in conn.execute("""SELECT pm.project_id,COUNT(*) n FROM project_monitors pm
        JOIN monitors m ON m.id=pm.monitor_id WHERE m.enabled=1 GROUP BY pm.project_id"""):
        if row["project_id"] in summaries: summaries[row["project_id"]]["active_monitors"] = row["n"]
    # The EXISTS branches preserve the trial_event identity when curation and
    # several linked monitors overlap. This aggregate returns all cards at once.
    for row in conn.execute("""SELECT p.id project_id,
        SUM(CASE WHEN te.severity='important' AND te.detected_at>=datetime('now','-7 days') THEN 1 ELSE 0 END) important,
        SUM(CASE WHEN te.severity='critical' AND te.detected_at>=datetime('now','-7 days') THEN 1 ELSE 0 END) critical,
        MAX(te.detected_at) last_activity
        FROM research_projects p JOIN trial_events te
        JOIN registry_records r ON r.record_id=te.record_id JOIN registry_sources s ON s.source_id=r.source_id
        WHERE EXISTS (SELECT 1 FROM project_trials pt WHERE pt.project_id=p.id AND pt.trial_id=r.source_trial_id AND pt.source=s.short_name)
           OR EXISTS (SELECT 1 FROM project_monitors pm JOIN monitor_events me ON me.monitor_id=pm.monitor_id
                      WHERE pm.project_id=p.id AND (me.related_change_event_id=te.event_id OR me.trial_id=r.source_trial_id))
           OR EXISTS (SELECT 1 FROM project_monitors pm JOIN monitor_trials mt ON mt.monitor_id=pm.monitor_id
                      WHERE pm.project_id=p.id AND mt.trial_id=r.source_trial_id AND mt.currently_matches=1)
        GROUP BY p.id"""):
        if row["project_id"] in summaries:
            p = summaries[row["project_id"]]
            p.update(recent_important_changes=row["important"], recent_critical_changes=row["critical"],
                     last_activity=row["last_activity"])
    relevant: dict[int, set[str]] = {pid: set() for pid in summaries}
    for row in conn.execute("SELECT project_id,source FROM project_trials"):
        if row["project_id"] in relevant: relevant[row["project_id"]].add(row["source"])
    for row in conn.execute("""SELECT DISTINCT pm.project_id,s.short_name FROM project_monitors pm
        JOIN monitor_trials mt ON mt.monitor_id=pm.monitor_id
        JOIN registry_records r ON r.source_trial_id=mt.trial_id JOIN registry_sources s ON s.source_id=r.source_id"""):
        if row["project_id"] in relevant: relevant[row["project_id"]].add(row["short_name"])
    available = {meta.short_name for meta in CONFIG.sources.values()}
    for row in conn.execute("""SELECT pm.project_id,mr.rules_json FROM project_monitors pm
        LEFT JOIN monitor_rules mr ON mr.monitor_id=pm.monitor_id"""):
        pid = row["project_id"]
        if pid not in relevant: continue
        try: selected = json.loads(row["rules_json"] or "{}").get("registries") or []
        except (TypeError, ValueError, AttributeError): selected = []
        relevant[pid].update({name for name in available if not selected or name.lower() in {str(s).lower() for s in selected}})
    health = registry_health()
    for pid, names in relevant.items():
        sources = [h for h in health if h["short_name"] in names]
        summaries[pid]["freshness"] = {"sources": sources,
            "affected": [h for h in sources if h["freshness"] in {"delayed", "stale"}]}
    return projects
