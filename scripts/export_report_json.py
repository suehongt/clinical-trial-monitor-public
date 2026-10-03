#!/usr/bin/env python3
"""Export trial data from the DB to JSON for the web viewer.

Modes:
  --all (recommended)   Write the multi-disease dataset used by the viewer:
                          web/public/data/index.json           disease list + counts
                          web/public/data/<key>.json           light trial list per profile
                          web/public/data/<key>_details.json   per-trial detail text
                          web/public/data/mirrors.json         cross-source mirror groups
  --profile <key>       Write a single profile payload to --out (legacy single-file mode)

Light list fields: id/source/title/status/phase/type/enrollment/dates/conditions/
sponsors/countries/url/primary endpoint.  Heavy detail text (summary, investigator,
endpoints detail, inclusion/exclusion) lives in <key>_details.json and is
lazy-loaded by the viewer when a detail modal is first opened.

Cross-source mirror groups come from record_master_map: every master trial whose
latest records span more than one registry source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DISEASE_PROFILES

DATA_DIR_DEFAULT = "web/public/data"
CDE = "CTR"


def clean_text(val):
    """Strip legacy HTML markers from free-text detail fields."""
    if not val:
        return None
    text = re.sub(r"<br\s*/?\s*>", "\n", str(val), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def arr(val):
    """Normalise the JSON-array-or-text storage to a clean list."""
    if not val:
        return []
    try:
        parsed = json.loads(val)
        items = parsed if isinstance(parsed, list) else [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        try:
            import ast
            items = ast.literal_eval(val)
            items = items if isinstance(items, list) else [str(items)]
        except (ValueError, SyntaxError):
            items = re.split(r"[;\n]", str(val))
    cleaned = []
    for item in items:
        if isinstance(item, dict):
            # structured sponsor entries -> plain name
            item = item.get("name") or json.dumps(item, ensure_ascii=False)
        item = re.sub(r"<br\s*/?\s*>", "; ", str(item), flags=re.I)
        item = re.sub(r"<[^>]+>", "", item)
        for part in re.split(r"\s*;\s*", item):
            part = part.strip()
            if part and part not in cleaned:
                cleaned.append(part)
    return cleaned


_EXCLUSION_MARKERS = re.compile(
    r"\n?\s*(Exclusion Criteria:?|排除标准[:：]?)\s*", re.I)


def split_embedded_exclusion(inc, exc):
    """Some ICTRP records have no separate exclusion_criteria field —
    the exclusion text (with its heading) is embedded at the end of
    inclusion_criteria.  Split at the marker."""
    if exc or not inc:
        return inc, exc
    m = _EXCLUSION_MARKERS.search(inc)
    if m:
        head, tail = inc[:m.start()].strip(), inc[m.end():].strip()
        if tail:
            return head, tail
    return inc, exc


def _detail_extractors():
    from ct_report.render import (_extract_brief_summary, _extract_investigator,
                                  _extract_ictrp_criteria, _eligibility_split)
    return _extract_brief_summary, _extract_investigator, _extract_ictrp_criteria, _eligibility_split


def build_detail(t, extractors):
    """Per-trial detail fields for the web viewer's detail modal."""
    brief_summary, investigator, ictrp_criteria, eligibility_split = extractors
    raw = t.get("raw_payload")
    summary = brief_summary(raw)
    inv = investigator(raw)
    if t["short_name"] == "ICTRP":
        inc, exc = ictrp_criteria(raw) or ("-", "-")
    else:
        inc, exc = eligibility_split(t.get("eligibility_criteria"))
    inc, exc = split_embedded_exclusion(inc, exc)
    return {
        "summary": clean_text(summary) if summary and summary != "-" else None,
        "investigator": clean_text(inv) if inv and inv != "-" else None,
        "inclusion": clean_text(inc) if inc and inc != "-" else None,
        "exclusion": clean_text(exc) if exc and exc != "-" else None,
    }


def build_light_trial(t) -> dict:
    """Light list-entry shaping for one query_trials row.

    Shared by the JSON exporter and the API server (web platform, Phase 7)
    so both emit the exact same Trial contract the viewer's guards validate.
    Heavy detail text (summary / criteria / investigator / endpoint tables)
    is deliberately NOT computed here — see build_detail().
    """
    # CTR: study_type text is lost at insert time (CDE labels like
    # "临床试验" are absent from the study_types lookup -> NULL FK).
    # Recover it by re-parsing the stored detail HTML.
    study_type = t.get("study_type_label")
    if not study_type and t["short_name"] == CDE:
        html = t.get("raw_payload") or ""
        if html.lstrip().startswith("<"):
            from collectors.chinadrugtrials import ChinaDrugTrialsCollector
            study_type = ChinaDrugTrialsCollector.parse_detail_html(
                html, t["source_trial_id"]).get("study_type")

    # CTR endpoint fields are flattened CDE tables (序号/指标/评价时间/…);
    # join the parsed cells back with newlines so the pre-line renderer
    # restores the table structure instead of cell-sized garbage bullets.
    secondary = arr(t.get("secondary_endpoints"))
    if t["short_name"] == CDE and secondary:
        secondary = ["\n".join(secondary)]

    return {
        "id": t["source_trial_id"],
        "source": t["short_name"],
        "title": t["title"],
        "scientificTitle": t.get("scientific_title"),
        "status": t.get("status_label") or "Unknown",
        "phase": t.get("study_phase"),
        "studyType": study_type,
        "enrollment": t.get("enrollment"),
        "registrationDate": t.get("registration_date"),
        "startDate": t.get("start_date"),
        "completionDate": t.get("completion_date"),
        "lastUpdated": t.get("last_updated_at_source"),
        "conditions": arr(t.get("conditions")),
        "sponsors": [s if isinstance(s, str) else s.get("name", "")
                     for s in arr(t.get("sponsors"))],
        "countries": arr(t.get("countries")),
        "url": t.get("source_url"),
        # Registry feeds (notably WHO ICTRP) sometimes preserve presentation
        # markup such as ``<br />`` and ``</p>`` inside outcome text.  The
        # React viewer deliberately renders registry content as text, so shape
        # it here instead of asking the UI to interpret untrusted HTML.
        "primaryEndpoint": clean_text(t.get("primary_endpoint")),
        "secondaryEndpoints": secondary,
        "interventions": arr(t.get("interventions")),
        "locations": arr(t.get("locations")),
    }


def build_endpoint_table(t) -> dict | None:
    """CDE/CTR flattened endpoint text -> structured endpointTable, or None
    for non-CDE sources.  Shared by the details-map export and the API's
    per-trial detail endpoint so both emit the same TrialDetailData shape.
    """
    if t.get("short_name") != CDE:
        return None
    from ct_report.render import parse_cde_endpoint_table
    secondary = arr(t.get("secondary_endpoints"))
    if secondary:
        secondary = ["\n".join(secondary)]
    return {
        "primary": parse_cde_endpoint_table(t.get("primary_endpoint")),
        "secondary": parse_cde_endpoint_table("\n".join(secondary)),
    }


def build_profile_payload(profile_key: str, limit: int = 0) -> tuple[dict, dict]:
    from ct_report.query import query_trials

    extractors = _detail_extractors()
    trials = query_trials(keywords=DISEASE_PROFILES[profile_key]["report_keywords"])
    if limit:
        trials = trials[:limit]

    light, details = [], {}
    for t in trials:
        detail = build_detail(t, extractors)
        entry = build_light_trial(t)
        light.append(entry)
        details[f"{t['short_name']}:{t['source_trial_id']}"] = {
            **detail,
            "endpointTable": build_endpoint_table(t),
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "profile": profile_key,
        "label": DISEASE_PROFILES[profile_key]["label"],
        "label_en": DISEASE_PROFILES[profile_key]["label_en"],
        "total": len(light),
        "trials": light,
    }
    return payload, details


def build_mirrors(conn=None) -> dict:
    """Cross-source mirror groups: masters whose latest records span >1 registry.

    conn: optional existing sqlite connection (the API server passes one per
    request so the thread-local cache in db.connection is never touched);
    defaults to db.connection.get_connection().
    """
    from db.connection import get_connection
    c = conn if conn is not None else get_connection()
    rows = c.execute("""
        SELECT rmm.master_trial_id AS master_id, s.short_name AS source,
               r.source_trial_id AS id, r.title, r.enrollment,
               st.label AS status_label, r.source_url
        FROM record_master_map rmm
        JOIN registry_records r ON r.record_id = rmm.record_id AND r.is_latest = 1
        JOIN registry_sources s ON s.source_id = r.source_id
        LEFT JOIN status_types st ON st.status_type_id = r.status_id
        ORDER BY rmm.master_trial_id, s.short_name
    """).fetchall()

    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["master_id"], {"trials": []})
        g["trials"].append({
            "source": r["source"], "id": r["id"],
            "title": r["title"], "status": r["status_label"],
            "enrollment": r["enrollment"], "url": r["source_url"],
        })
    mirrors = [
        {"masterId": mid, "sources": sorted({t["source"] for t in g["trials"]}),
         "trials": g["trials"]}
        for mid, g in groups.items()
        if len({t["source"] for t in g["trials"]}) > 1
    ]
    mirrors.sort(key=lambda m: (-len(m["sources"]), m["masterId"]))
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total": len(mirrors),
        "groups": mirrors,
    }


def build_events(limit: int = 0, conn=None) -> dict:
    """Recent field-level change events (trial_events, newest first).

    status_id FK values are translated to status labels (same as the
    per-record history — the global timeline must not show bare integers).

    conn: optional existing sqlite connection (see build_mirrors).
    """
    from ct_report.query import translate_status_event_values
    from db.connection import get_connection
    c = conn if conn is not None else get_connection()
    sql = """
        SELECT te.detected_at, te.field_name, te.old_value, te.new_value,
               te.change_category, te.severity, te.change_type,
               te.importance_score, r.source_trial_id AS id,
               s.short_name AS source, r.title, r.source_url
        FROM trial_events te
        JOIN registry_records r ON r.record_id = te.record_id
        JOIN registry_sources s ON s.source_id = r.source_id
        ORDER BY te.detected_at DESC, te.event_id DESC
    """
    params = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    rows = translate_status_event_values(rows, c)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total": len(rows),
        "events": rows,
    }


def write_csv(path: Path, trials: list) -> None:
    """Write the light trial list as an Excel-friendly CSV (UTF-8 BOM)."""
    import csv
    columns = ["id", "source", "title", "scientificTitle", "status", "phase",
               "studyType", "enrollment", "registrationDate", "startDate",
               "completionDate", "lastUpdated", "conditions", "sponsors",
               "countries", "interventions", "locations", "primaryEndpoint",
               "secondaryEndpoints", "url"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for t in trials:
            w.writerow([
                t.get(c) if c != "conditions" and c != "sponsors"
                and c != "countries" and c != "interventions"
                and c != "locations" and c != "secondaryEndpoints"
                else "; ".join(t.get(c) or [])
                for c in columns
            ])


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def export_all(data_dir: Path, limit: int = 0) -> None:
    index = {"generated_at": datetime.now(timezone.utc)
             .strftime("%Y-%m-%d %H:%M:%S UTC"), "diseases": []}
    for key in DISEASE_PROFILES:
        payload, details = build_profile_payload(key, limit)
        write_json(data_dir / f"{key}.json", payload)
        write_json(data_dir / f"{key}_details.json", details)
        index["diseases"].append({
            "key": key, "label": payload["label"],
            "label_en": payload["label_en"], "total": payload["total"],
            "file": f"{key}.json",
        })
        print(f"  {key}: {payload['total']} trials "
              f"({(data_dir / f'{key}.json').stat().st_size / 1e6:.1f} MB "
              f"+ details {(data_dir / f'{key}_details.json').stat().st_size / 1e6:.1f} MB)",
              flush=True)

    mirrors = build_mirrors()
    write_json(data_dir / "mirrors.json", mirrors)
    print(f"  mirrors: {mirrors['total']} cross-source groups", flush=True)

    events = build_events()
    write_json(data_dir / "events.json", events)
    print(f"  events: {events['total']} change events", flush=True)

    for key in DISEASE_PROFILES:
        trials = json.loads((data_dir / f"{key}.json").read_text(encoding="utf-8"))["trials"]
        write_csv(data_dir / f"{key}.csv", trials)
        print(f"  {key}.csv: {(data_dir / f'{key}.csv').stat().st_size / 1e6:.1f} MB", flush=True)

    write_json(data_dir / "index.json", index)
    print(f"data set written to {data_dir}/ ({len(DISEASE_PROFILES)} profiles, "
          f"index + mirrors + events + csv)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default=os.environ.get("CT_DISEASE_PROFILE", "mi"),
                        help="Single-profile mode: disease profile key")
    parser.add_argument("--out", default="web/public/report_data.json",
                        help="Single-profile mode output path")
    parser.add_argument("--all", action="store_true",
                        help="Multi-disease mode: write index + per-profile files + mirrors")
    parser.add_argument("--data-dir", default=DATA_DIR_DEFAULT,
                        help="Multi-disease mode output directory")
    parser.add_argument("--limit", type=int, default=0, help="Cap trials per profile (0 = all)")
    args = parser.parse_args()

    if args.all:
        export_all(Path(args.data_dir), args.limit)
    else:
        payload, details = build_profile_payload(args.profile, args.limit)
        write_json(Path(args.out), payload)
        side = Path(args.out).with_name(Path(args.out).stem + "_details.json")
        write_json(side, details)
        print(f"exported {payload['total']} trials ({payload['label_en']}) -> "
              f"{args.out} ({Path(args.out).stat().st_size / 1e6:.1f} MB) "
              f"+ details {side.name} ({side.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
