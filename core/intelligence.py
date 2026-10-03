"""Deterministic, source-linked trial intelligence over persisted local facts.

Event counts use trial_events.event_id as identity.  Monitor membership events
are a different unit and remain per-monitor.  This module performs no writes,
matching, external requests, or narrative inference.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from config import CONFIG
from core.ingestion import registry_health

WINDOW_DAYS = {"24h": 1, "7d": 7, "30d": 30}
SEVERITIES = ("critical", "important", "normal", "minor")
SEVERITY_RANK = {name: 4 - i for i, name in enumerate(SEVERITIES)}
FIELD_CATEGORIES = {
    "status_id": "recruitment", "enrollment": "enrollment",
    "primary_endpoint": "outcomes", "secondary_endpoints": "outcomes",
    "start_date": "timeline", "primary_completion_date": "timeline", "completion_date": "timeline",
    "sponsors": "sponsor", "countries": "geography", "locations": "geography",
    "interventions": "intervention", "eligibility_criteria": "eligibility",
}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _array(value: Any) -> list[str]:
    if not value:
        return []
    try:
        items = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v).strip() for v in items if isinstance(v, str) and v.strip()] if isinstance(items, list) else []


def _status(value: Any, labels: dict[str, str]) -> str:
    return labels.get(str(value), str(value or "Unknown"))


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("event_id", "trial_id", "source", "title", "field_name", "old_value", "new_value", "severity", "detected_at")}


def _source_filter(registry: str | None) -> str | None:
    if not registry or registry == "all":
        return None
    if registry in CONFIG.sources:
        return CONFIG.sources[registry].short_name
    raise ValueError("unknown registry")


def _time_bounds(window: str, now: datetime, start: str | None, end: str | None) -> tuple[str, str]:
    if window == "custom":
        if not start or not end:
            raise ValueError("custom window requires start and end dates")
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("invalid custom date") from exc
        if first > last or (last - first).days > 366:
            raise ValueError("invalid custom date range")
        return f"{first} 00:00:00", f"{last + timedelta(days=1)} 00:00:00"
    if window not in WINDOW_DAYS:
        raise ValueError("unsupported window")
    # SQLite timestamps are second precision.  Include records written in the
    # current second while preserving a half-open interval in every query.
    return _stamp(now - timedelta(days=WINDOW_DAYS[window])), _stamp(now + timedelta(seconds=1))


def _scope_sql(scope: str, monitor_id: int | None, alias: str = "r",
               project_id: int | None = None, current: bool = False) -> tuple[str, list[Any]]:
    trial = f"{alias}.source_trial_id"
    if scope == "project":
        if project_id is None:
            raise ValueError("project_id is required")
        from core.projects import project_current_clause, project_event_clause
        return (project_current_clause(project_id, record=alias) if current else
                project_event_clause(project_id, record=alias))
    if scope == "all":
        return "1=1", []
    if scope == "watched":
        return f"EXISTS (SELECT 1 FROM trial_watches w WHERE w.trial_id={trial} AND w.enabled=1)", []
    if scope == "monitor":
        if monitor_id is None:
            raise ValueError("monitor_id is required")
        return (f"(EXISTS (SELECT 1 FROM monitor_trials mt WHERE mt.monitor_id=? AND mt.trial_id={trial} AND mt.currently_matches=1) "
                f"OR EXISTS (SELECT 1 FROM monitor_events me WHERE me.monitor_id=? AND me.trial_id={trial}))", [monitor_id, monitor_id])
    if scope == "monitored":
        return (f"(EXISTS (SELECT 1 FROM monitor_trials mt JOIN monitors m ON m.id=mt.monitor_id WHERE m.enabled=1 AND mt.trial_id={trial} AND mt.currently_matches=1) "
                f"OR EXISTS (SELECT 1 FROM monitor_events me JOIN monitors m ON m.id=me.monitor_id WHERE m.enabled=1 AND me.trial_id={trial}))", [])
    raise ValueError("unsupported scope")


def _event_query(scope: str, monitor_id: int | None, short_name: str | None,
                 first: str, last: str, project_id: int | None = None) -> tuple[str, list[Any]]:
    clause, scope_args = _scope_sql(scope, monitor_id, project_id=project_id)
    args: list[Any] = [first, last] + scope_args
    source_clause = ""
    if short_name:
        source_clause = " AND s.short_name=?"
        args.append(short_name)
    base = f"""FROM trial_events te JOIN registry_records r ON r.record_id=te.record_id
        JOIN registry_sources s ON s.source_id=r.source_id
        WHERE te.detected_at >= ? AND te.detected_at < ? AND {clause}{source_clause}"""
    return base, args


def _event_rows(conn: sqlite3.Connection, scope: str, monitor_id: int | None,
                short_name: str | None, first: str, last: str, project_id: int | None = None) -> list[dict[str, Any]]:
    base, args = _event_query(scope, monitor_id, short_name, first, last, project_id)
    return [dict(r) for r in conn.execute(f"""SELECT te.event_id, te.field_name, te.old_value, te.new_value,
        te.change_category, COALESCE(te.severity,'normal') severity, te.detected_at,
        r.source_trial_id trial_id, r.title, r.sponsors, r.countries, s.short_name source
        {base}
        ORDER BY te.detected_at DESC, te.event_id DESC""", args)]


def _event_aggregates(conn: sqlite3.Connection, scope: str, monitor_id: int | None,
                      short_name: str | None, first: str, last: str, project_id: int | None = None) -> tuple[dict[str, int], dict[str, Counter]]:
    """Primary change counts and chart buckets use grouped database queries."""
    base, args = _event_query(scope, monitor_id, short_name, first, last, project_id)
    totals = conn.execute(f"SELECT COUNT(te.event_id), COUNT(DISTINCT r.source_trial_id) {base}", args).fetchone()
    severity = Counter({r["severity"]: r["n"] for r in conn.execute(
        f"SELECT COALESCE(te.severity,'normal') severity, COUNT(*) n {base} GROUP BY COALESCE(te.severity,'normal')", args)})
    by_day: dict[str, Counter] = defaultdict(Counter)
    for row in conn.execute(f"SELECT date(te.detected_at) day, COALESCE(te.severity,'normal') severity, COUNT(*) n {base} GROUP BY date(te.detected_at), COALESCE(te.severity,'normal')", args):
        by_day[row["day"]][row["severity"]] = row["n"]
    return {"change_events": totals[0], "changed_trials": totals[1], **severity}, by_day


def _membership_rows(conn: sqlite3.Connection, scope: str, monitor_id: int | None,
                     short_name: str | None, first: str, last: str, project_id: int | None = None) -> list[dict[str, Any]]:
    if scope == "watched" or scope == "all":
        return []
    monitor_clause = ("me.monitor_id=?" if scope == "monitor" else
                      "EXISTS (SELECT 1 FROM project_monitors pm WHERE pm.project_id=? AND pm.monitor_id=me.monitor_id)" if scope == "project" else "m.enabled=1")
    args: list[Any] = [first, last]
    if scope in {"monitor", "project"}:
        args.append(monitor_id if scope == "monitor" else project_id)
    source_clause = ""
    if short_name:
        source_clause = " AND EXISTS (SELECT 1 FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id WHERE r.source_trial_id=me.trial_id AND s.short_name=?)"
        args.append(short_name)
    return [dict(r) for r in conn.execute(f"""SELECT me.id, me.monitor_id, m.name monitor_name, me.trial_id,
        me.event_type, me.detected_at FROM monitor_events me JOIN monitors m ON m.id=me.monitor_id
        WHERE me.detected_at>=? AND me.detected_at<? AND {monitor_clause}
        AND me.event_type IN ('trial_entered','trial_left','trial_reentered'){source_clause}
        ORDER BY me.detected_at DESC, me.id DESC""", args)]


def _current_rows(conn: sqlite3.Connection, scope: str, monitor_id: int | None,
                  short_name: str | None, project_id: int | None = None) -> list[dict[str, Any]]:
    if scope == "monitor":
        clause, args = "EXISTS (SELECT 1 FROM monitor_trials mt WHERE mt.monitor_id=? AND mt.trial_id=r.source_trial_id AND mt.currently_matches=1)", [monitor_id]
    elif scope == "monitored":
        clause, args = "EXISTS (SELECT 1 FROM monitor_trials mt JOIN monitors m ON m.id=mt.monitor_id WHERE m.enabled=1 AND mt.trial_id=r.source_trial_id AND mt.currently_matches=1)", []
    else:
        clause, args = _scope_sql(scope, monitor_id, project_id=project_id, current=True)
    source_clause = ""
    if short_name:
        source_clause = " AND s.short_name=?"
        args.append(short_name)
    return [dict(r) for r in conn.execute(f"""SELECT r.source_trial_id trial_id, r.title, r.first_crawled_at,
        r.is_bootstrap, r.study_phase, r.sponsors, r.countries, s.short_name source
        FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id
        WHERE r.is_latest=1 AND {clause}{source_clause}""", args)]


def _freshness(short_name: str | None, now: datetime, relevant: set[str] | None = None) -> dict[str, Any]:
    sources = [r for r in registry_health(now) if (short_name is None or r["short_name"] == short_name)
               and (relevant is None or r["short_name"] in relevant)]
    affected = [r for r in sources if r["freshness"] in {"delayed", "stale"}]
    unavailable = [r for r in sources if r["freshness"] == "unknown"]
    disabled = [r for r in sources if r["freshness"] == "disabled"]
    return {"sources": sources, "affected": [{"registry": r["registry"], "name": r["full_name"], "state": r["freshness"], "reason": r["stale_reason"]} for r in affected],
            "unknown": [{"registry": r["registry"], "name": r["full_name"]} for r in unavailable],
            "disabled": [{"registry": r["registry"], "name": r["full_name"]} for r in disabled],
            "complete": any(r["freshness"] == "fresh" for r in sources) and not affected and not unavailable}


def _cross_source_disagreements(conn: sqlite3.Connection, trial_ids: set[str]) -> dict[str, Any]:
    """Compare linked latest source records; no source is deemed authoritative."""
    if not trial_ids:
        return {"logical_trials": 0, "difference_fields": 0, "fields": {}, "items": []}
    linked: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute("""SELECT map.master_trial_id, r.source_trial_id, r.title,
        s.short_name source, st.label status, r.enrollment, r.completion_date
        FROM record_master_map map JOIN registry_records r ON r.record_id=map.record_id
        JOIN registry_sources s ON s.source_id=r.source_id
        LEFT JOIN status_types st ON st.status_type_id=r.status_id
        WHERE r.is_latest=1 AND map.match_status='AUTO_CONFIRMED'"""):
        if row["source_trial_id"] in trial_ids:
            linked[row["master_trial_id"]].append(dict(row))
    items = []
    fields = Counter()
    for master_id, records in linked.items():
        if len({r["source"] for r in records}) < 2:
            continue
        differing = [field for field in ("status", "enrollment", "completion_date")
                     if len({str(r[field]) for r in records if r[field] is not None}) > 1]
        if differing:
            fields.update(differing)
            items.append({"master_trial_id": master_id, "fields": differing,
                          "sources": [{"source": r["source"], "trial_id": r["source_trial_id"], "title": r["title"]} for r in records]})
    return {"logical_trials": len(items), "difference_fields": sum(fields.values()),
            "fields": dict(fields), "items": items[:20]}


def overview(conn: sqlite3.Connection, *, scope: str = "monitored", monitor_id: int | None = None,
             window: str = "7d", registry: str | None = None, start: str | None = None,
             end: str | None = None, now: datetime | None = None, project_id: int | None = None) -> dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    first, last = _time_bounds(window, current_time, start, end)
    short = _source_filter(registry)
    if scope == "monitor" and monitor_id is None:
        raise ValueError("monitor_id is required")
    if scope == "monitor" and conn.execute("SELECT 1 FROM monitors WHERE id=?", (monitor_id,)).fetchone() is None:
        raise LookupError("monitor not found")
    if scope == "project":
        if project_id is None: raise ValueError("project_id is required")
        from core.projects import project_row
        project_row(conn, project_id)
    events = _event_rows(conn, scope, monitor_id, short, first, last, project_id)
    event_totals, by_day = _event_aggregates(conn, scope, monitor_id, short, first, last, project_id)
    members = _membership_rows(conn, scope, monitor_id, short, first, last, project_id)
    current = _current_rows(conn, scope, monitor_id, short, project_id)
    labels = {str(r[0]): r[1] for r in conn.execute("SELECT status_type_id,label FROM status_types")}
    severity = Counter({s: event_totals.get(s, 0) for s in SEVERITIES})
    categories = Counter(FIELD_CATEGORIES.get(e["field_name"], e["change_category"] or "other") for e in events)
    status = []
    outcomes = []
    enrollment = []
    timeline = []
    geography = []
    changed_ids = {e["trial_id"] for e in events}
    for e in events:
        field = e["field_name"]
        item = _identity(e)
        if field == "status_id":
            item["old_label"] = _status(e["old_value"], labels)
            item["new_label"] = _status(e["new_value"], labels)
            status.append(item)
        elif field in {"primary_endpoint", "secondary_endpoints"}:
            outcomes.append(item)
        elif field == "enrollment":
            try:
                old, new = int(str(e["old_value"])), int(str(e["new_value"]))
            except (TypeError, ValueError):
                continue
            item.update({"old_number": old, "new_number": new, "delta": new - old,
                         "percent": round((new - old) * 100 / old, 1) if old > 0 else None})
            enrollment.append(item)
        elif field in {"start_date", "primary_completion_date", "completion_date"}:
            try:
                delta_days = (date.fromisoformat(str(e["new_value"])[:10]) - date.fromisoformat(str(e["old_value"])[:10])).days
            except ValueError:
                continue
            item.update({"delta_days": delta_days, "direction": "delayed" if delta_days > 0 else "earlier" if delta_days < 0 else "unchanged"})
            timeline.append(item)
        elif field == "countries":
            old, new = set(_array(e["old_value"])), set(_array(e["new_value"]))
            if old or new:
                item.update({"added": sorted(new - old), "removed": sorted(old - new)})
                geography.append(item)
    transitions = Counter((e["old_label"], e["new_label"]) for e in status)
    status_groups = [{"from": old, "to": new, "count": n,
                      "items": [e for e in status if e["old_label"] == old and e["new_label"] == new][:20]}
                     for (old, new), n in transitions.most_common()]
    per_trial: dict[str, dict[str, Any]] = {}
    for e in events:
        key = e["trial_id"]
        if key not in per_trial:
            per_trial[key] = {"trial_id": key, "title": e["title"], "source": e["source"], "count": 0,
                              "highest_severity": e["severity"], "latest_change": e["detected_at"], "items": []}
        row = per_trial[key]
        row["count"] += 1
        if SEVERITY_RANK.get(e["severity"], 0) > SEVERITY_RANK.get(row["highest_severity"], 0):
            row["highest_severity"] = e["severity"]
        if len(row["items"]) < 20:
            row["items"].append(_identity(e))
    sponsor: dict[str, dict[str, Any]] = defaultdict(lambda: {"new_trials": set(), "changed_trials": set(), "priority_events": set()})
    new_rows = [r for r in current if not r["is_bootstrap"] and first <= r["first_crawled_at"] < last]
    new_ids = {r["trial_id"] for r in new_rows}
    priority_by_trial: dict[str, set[int]] = defaultdict(set)
    for e in events:
        if e["severity"] in {"critical", "important"}:
            priority_by_trial[e["trial_id"]].add(e["event_id"])
    for r in current:
        for name in _array(r["sponsors"]):
            if r["trial_id"] in new_ids:
                sponsor[name]["new_trials"].add(r["trial_id"])
            if r["trial_id"] in changed_ids:
                sponsor[name]["changed_trials"].add(r["trial_id"])
                sponsor[name]["priority_events"].update(priority_by_trial[r["trial_id"]])
    sponsors = [{"name": name, "new_trials": len(v["new_trials"]), "changed_trials": len(v["changed_trials"]),
                 "priority_changes": len(v["priority_events"])} for name, v in sponsor.items()
                if v["new_trials"] or v["changed_trials"] or v["priority_events"]]
    sponsors.sort(key=lambda r: (-r["priority_changes"], -r["changed_trials"], r["name"]))
    member_counts = Counter(e["event_type"] for e in members)
    source_activity = []
    for source in sorted({r["source"] for r in current} | {e["source"] for e in events}):
        source_activity.append({"source": source, "new_trials": len({r["trial_id"] for r in new_rows if r["source"] == source}),
                                "changed_trials": len({e["trial_id"] for e in events if e["source"] == source}),
                                "priority_changes": sum(e["source"] == source and e["severity"] in {"critical", "important"} for e in events)})
    dates = []
    day = date.fromisoformat(first[:10]); last_day = date.fromisoformat((datetime.fromisoformat(last) - timedelta(seconds=1)).date().isoformat())
    member_by_day = defaultdict(Counter)
    for e in members:
        member_by_day[e["detected_at"][:10]][e["event_type"]] += 1
    while day <= last_day:
        key = day.isoformat()
        dates.append({"date": key, **{s: by_day[key][s] for s in SEVERITIES},
                      "entered": member_by_day[key]["trial_entered"], "left": member_by_day[key]["trial_left"],
                      "reentered": member_by_day[key]["trial_reentered"]})
        day += timedelta(days=1)
    relevant = ({r["source"] for r in current} | {e["source"] for e in events}) if scope == "project" else None
    if relevant is not None:
        for rule in conn.execute("""SELECT mr.rules_json FROM project_monitors pm
            LEFT JOIN monitor_rules mr ON mr.monitor_id=pm.monitor_id WHERE pm.project_id=?""", (project_id,)):
            try:
                selected = json.loads(rule[0] or "{}").get("registries") or []
            except (TypeError, ValueError, AttributeError):
                selected = []
            available = {meta.short_name for meta in CONFIG.sources.values()}
            relevant.update({name for name in available if not selected or name.lower() in {str(s).lower() for s in selected}})
    fresh = _freshness(short, current_time, relevant)
    disagreements = _cross_source_disagreements(conn, {r["trial_id"] for r in current})
    unique_new = list({r["trial_id"]: r for r in new_rows}.values())
    current_by_id: dict[str, dict[str, Any]] = {}
    for row in sorted(current, key=lambda r: (r["source"] == "ICTRP", r["source"])):
        current_by_id.setdefault(row["trial_id"], row)
    unique_current = list(current_by_id.values())
    counts = {"current_trials": len(unique_current), "new_trials": len(unique_new),
              "changed_trials": event_totals["changed_trials"], "change_events": event_totals["change_events"],
              "critical_changes": severity["critical"], "important_changes": severity["important"],
              "entered": member_counts["trial_entered"], "left": member_counts["trial_left"],
              "reentered": member_counts["trial_reentered"], "status_transitions": len(status),
              "outcome_changes": len(outcomes), "outcome_changed_trials": len({e["trial_id"] for e in outcomes}),
              "enrollment_changes": len(enrollment), "timeline_changes": len(timeline),
              "active_sponsors": len(sponsors), "country_changes": len(geography)}
    return {"scope": scope, "monitor_id": monitor_id, "project_id": project_id, "window": window, "start": first, "end": last,
            "registry": registry or "all", "counts": counts, "severity": {s: severity[s] for s in SEVERITIES},
            "categories": dict(categories), "new_trials": [{"trial_id": r["trial_id"], "title": r["title"], "source": r["source"]} for r in unique_new[:20]],
            "monitor_activity": members[:20], "status_transitions": status_groups, "outcome_changes": outcomes[:20],
            "enrollment_changes": sorted(enrollment, key=lambda e: abs(e["delta"]), reverse=True)[:20],
            "enrollment_increases": sorted((e for e in enrollment if e["delta"] > 0), key=lambda e: -e["delta"])[:10],
            "enrollment_decreases": sorted((e for e in enrollment if e["delta"] < 0), key=lambda e: e["delta"])[:10],
            "timeline_changes": timeline[:20], "country_changes": geography[:20], "sponsors": sponsors[:20],
            "phase_distribution": dict(Counter(r["study_phase"] or "Unknown" for r in unique_current)),
            "source_activity": source_activity, "most_frequently_updated": sorted(per_trial.values(), key=lambda r: (-r["count"], r["trial_id"]))[:10],
            "priority_events": [_identity(e) for e in events if e["severity"] in {"critical", "important"}][:20],
            "trends": dates, "freshness": fresh, "cross_source_disagreements": disagreements}


def briefing(data: dict[str, Any]) -> dict[str, Any]:
    """Portable structured briefing with a deterministic English fallback."""
    c = data["counts"]
    period = {"24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days"}.get(data["window"], "selected period")
    summary = (f"During the {period}: newly indexed trials {c['new_trials']}; "
               f"trials with meaningful changes {c['changed_trials']}; change events {c['change_events']}; "
               f"critical {c['critical_changes']}; important {c['important_changes']}; "
               f"monitor entries {c['entered']}; exits {c['left']}.")
    sections = [
        {"key": "new_entered", "title": "New / Entered Trials", "count": c["new_trials"] + c["entered"] + c["reentered"],
         "new_trials": data["new_trials"], "membership": [m for m in data["monitor_activity"] if m["event_type"] in {"trial_entered", "trial_reentered"}]},
        {"key": "priority", "title": "Important Trial Changes", "count": c["critical_changes"] + c["important_changes"], "items": data["priority_events"]},
        {"key": "recruitment", "title": "Recruitment Status Changes", "count": c["status_transitions"], "groups": data["status_transitions"]},
        {"key": "outcomes", "title": "Outcome Changes", "count": c["outcome_changes"], "items": data["outcome_changes"]},
        {"key": "enrollment_timeline", "title": "Enrollment / Timeline Changes", "count": c["enrollment_changes"] + c["timeline_changes"],
         "enrollment": data["enrollment_changes"], "increases": data["enrollment_increases"],
         "decreases": data["enrollment_decreases"], "timeline": data["timeline_changes"]},
        {"key": "sponsor_geography", "title": "Sponsor / Geographic Activity", "count": c["active_sponsors"] + c["country_changes"],
         "sponsors": data["sponsors"], "countries": data["country_changes"]},
        {"key": "monitor_activity", "title": "Monitor Activity", "count": c["entered"] + c["left"] + c["reentered"], "items": data["monitor_activity"]},
        {"key": "cross_source", "title": "Cross-source Disagreements", "count": data["cross_source_disagreements"]["logical_trials"],
         "disagreements": data["cross_source_disagreements"]},
        {"key": "freshness", "title": "Source Freshness", "count": len(data["freshness"]["sources"]),
         "sources": [{"name": s["full_name"], "state": s["freshness"], "last_success": s["last_successful_sync"],
                      "stale_reason": s["stale_reason"], "data_as_of": s["data_as_of"]} for s in data["freshness"]["sources"]]},
    ]
    return {"filters": {k: data[k] for k in ("scope", "monitor_id", "project_id", "window", "start", "end", "registry")},
            "counts": c, "summary": summary, "sections": [s for s in sections if s["count"] > 0],
            "freshness": data["freshness"], "trends": data["trends"], "categories": data["categories"],
            "cross_source_disagreements": data["cross_source_disagreements"],
            "source_activity": data["source_activity"],
            "most_frequently_updated": data["most_frequently_updated"]}


def monitor_comparison(conn: sqlite3.Connection, *, window: str = "7d", registry: str | None = None,
                       now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    first, last = _time_bounds(window, current, None, None)
    short = _source_filter(registry)
    result = []
    for monitor in conn.execute("SELECT id,name,last_checked_at FROM monitors WHERE enabled=1 ORDER BY name,id"):
        mid = monitor["id"]
        members = _membership_rows(conn, "monitor", mid, short, first, last)
        events = _event_rows(conn, "monitor", mid, short, first, last)
        current_rows = _current_rows(conn, "monitor", mid, short)
        types = Counter(m["event_type"] for m in members)
        result.append({"monitor_id": mid, "name": monitor["name"], "current_trials": len({r["trial_id"] for r in current_rows}),
                       "entered": types["trial_entered"], "left": types["trial_left"], "reentered": types["trial_reentered"],
                       "changed_trials": len({e["trial_id"] for e in events}),
                       "critical": sum(e["severity"] == "critical" for e in events),
                       "important": sum(e["severity"] == "important" for e in events),
                       "last_run": monitor["last_checked_at"], "freshness": _freshness(short, current)})
    return result
