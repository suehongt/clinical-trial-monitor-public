"""Topic-monitor membership over existing normalized current records/events."""
from __future__ import annotations
import json
import logging
import re
import sqlite3
from typing import Any, Dict, List

SUPPORTED_RULES = {"query", "registries", "statuses", "study_types", "phase", "country", "sponsor", "condition", "intervention"}
logger = logging.getLogger(__name__)

# Phase tokens: English "Phase II" (romans before singles so "iii" wins),
# Arabic "Phase 2", and CJK "Ⅱ期"/"2 期" as stored by ChiCTR/CTR.
_PHASE_NUM = r"(iv|iii|ii|i|Ⅳ|Ⅲ|Ⅱ|Ⅰ|0|1|2|3|4)"
# "Phase 1/2", "Phase 1 / Phase 2", "Phase II-III": join the continuation
# number back onto a "phase" keyword so the single-token scan below sees
# every member of a combination.  The lookahead refuses non-phase numbers,
# so "Phase 1 - see protocol" is not joined.
_PHASE_JOIN_RE = re.compile(r"(phase\s*" + _PHASE_NUM + r")\s*(?:/|&|,|-|to\b)\s*(?=" + _PHASE_NUM + r"\b|phase\s*" + _PHASE_NUM + ")", re.IGNORECASE)
_PHASE_MAIN_RE = re.compile(r"phase\s*" + _PHASE_NUM + r"\b", re.IGNORECASE)
_PHASE_CJK_RE = re.compile(r"([ⅣⅢⅡⅠ])\s*期|([0-4])\s*期")
_PHASE_NUMBERS = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "Ⅳ": "4", "Ⅲ": "3", "Ⅱ": "2", "Ⅰ": "1"}
_PHASE_NA = {"n/a", "na", "not applicable", "not selected", "unknown", ""}


def normalize_phase(raw: Any) -> str:
    """Bucket a raw study_phase string into one canonical facet label.

    Registries disagree wildly (ICTRP concatenates full combination strings,
    CTR writes Ⅱ期, NCT writes "Phase 2/Phase 3"), which previously produced
    unusable one-count facet chips.  Buckets are the shared vocabulary of the
    search facet, the phase filter, and saved monitor rules; callers compare
    buckets instead of raw strings (see _phase_ok) or display them directly.
    """
    text = str(raw or "").strip()
    if text.lower() in _PHASE_NA:
        return "N/A"
    numbers: List[str] = []

    def push(token: str) -> None:
        number = (_PHASE_NUMBERS.get(token)
                  or _PHASE_NUMBERS.get(token.lower())
                  or _PHASE_NUMBERS.get(token.upper())
                  or token)
        if number not in numbers:
            numbers.append(number)

    for match in _PHASE_MAIN_RE.finditer(_PHASE_JOIN_RE.sub(lambda m: m.group(1) + " phase ", text)):
        push(match.group(1))
    for match in _PHASE_CJK_RE.finditer(text):
        push(match.group(1) or match.group(2))
    if not numbers:
        return "Other"
    if len(numbers) == 1:
        return f"Phase {numbers[0]}"
    return "Phase " + "/".join(numbers)


def validate_rules(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) - SUPPORTED_RULES:
        raise ValueError("invalid monitor rules")
    for key, item in value.items():
        if isinstance(item, str):
            if len(item) > (500 if key == "query" else 200):
                raise ValueError(f"invalid {key} rule")
        elif isinstance(item, list):
            if len(item) > 20 or any(not isinstance(part, str) or len(part) > 200 for part in item):
                raise ValueError(f"invalid {key} rule")
        else:
            raise ValueError(f"invalid {key} rule")
    return value

def _terms(value: Any) -> List[str]:
    if isinstance(value, str): return [value.lower()] if value.strip() else []
    return [str(x).lower() for x in value or [] if str(x).strip()]

def matching_trials(conn: sqlite3.Connection, rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return current records matching a monitor rule.

    This is deliberately the single evaluator used by saved monitors and the
    read-only discovery API.  A rule is ANDed across fields and ORed within a
    field; values are normalized only for comparison, never persisted here.
    Rows carry the exporter's light-shaping keys (status_label /
    study_type_label) so search results and monitor output share one Trial
    contract.
    """
    # Free-text searches used to materialize every latest record (including
    # large JSON/text columns) in Python before rejecting almost all of them.
    # That is tolerable in small test databases but exhausts the demo server's
    # memory on a production-sized registry. Push the exact same case-insensitive
    # substring/AND predicate into SQLite first; the Python predicate below is
    # intentionally retained as the semantic authority and safety net.
    query = _terms(rules.get("query"))
    query_words = [w for t in query for w in t.split()]
    where = ["r.is_latest=1"]
    params: List[str] = []
    searchable = "lower(" + " || ' ' || ".join(
        f"COALESCE(r.{field}, '')" for field in (
            "source_trial_id", "title", "scientific_title", "conditions",
            "interventions", "sponsors",
        )
    ) + ")"
    for word in query_words:
        # SQLite's built-in lower() is ASCII-only. CJK/uncased text is safe,
        # but applying this prefilter to e.g. "é" could exclude an uppercase
        # "É" row that Python's Unicode lower() would match. Leave those rare
        # cased non-ASCII words to the authoritative Python predicate.
        if any(ord(char) >= 128 and char.lower() != char.upper()
               for char in word):
            continue
        where.append(f"instr({searchable}, ?) > 0")
        params.append(word)

    rows = conn.execute(f"""SELECT r.*, s.short_name,
        st.label AS status_label, ty.label AS study_type_label
        FROM registry_records r JOIN registry_sources s ON s.source_id=r.source_id
        LEFT JOIN status_types st ON st.status_type_id=r.status_id LEFT JOIN study_types ty ON ty.study_type_id=r.study_type_id
        WHERE {' AND '.join(where)}""", params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        def contains(field: str, terms: List[str]) -> bool:
            return not terms or any(t in (d.get(field) or "").lower() for t in terms)
        registries = _terms(rules.get("registries")); statuses = _terms(rules.get("statuses")); types = _terms(rules.get("study_types"))
        # Query terms are whitespace-split inside each rule value, then ANDed:
        # "myocarditis CAR-T" must contain both words, so the same free-text
        # query works for discovery search and saved monitors alike (CJK
        # phrases stay one term — no spaces to split on).
        if query_words and not all(w in " ".join(str(d.get(k) or "") for k in ("source_trial_id","title","scientific_title","conditions","interventions","sponsors")).lower() for w in query_words): continue
        if registries and d["short_name"].lower() not in registries: continue
        if statuses and (d.get("status_label") or "").lower() not in statuses: continue
        if types and (d.get("study_type_label") or "").lower() not in types: continue
        countries = _terms(rules.get("country"))
        # A canonical China filter must match both English and Chinese source
        # country labels; this is a comparison alias, never a registry filter.
        countries = [alias for term in countries for alias in ({"china", "中国"} if term in {"china", "中国"} else {term})]
        if not all((_phase_ok(d.get("study_phase"), _terms(rules.get("phase"))), contains("countries", countries), contains("sponsors", _terms(rules.get("sponsor"))), contains("conditions", _terms(rules.get("condition"))), contains("interventions", _terms(rules.get("intervention"))))): continue
        result.append(d)
    return result


def _phase_ok(raw: Any, terms: List[str]) -> bool:
    """Phase rule semantics: substring match (historical) plus bucket equality.

    Substring keeps every saved rule working ("phase 2" still matches
    "Phase 1/Phase 2").  Bucket equality adds what substring can never express:
    the canonical facet buckets ("Phase 1/2", "Other", "N/A") now emitted by
    the search facets, including ICTRP combination strings and roman numerals.
    A non-"other" rule bucket matches equal buckets; the literal "other" rule
    selects the Other bucket, while free terms that merely normalize to Other
    (e.g. a CTR category name) deliberately stay substring-only so they cannot
    balloon into every uncategorized trial.
    """
    if not terms:
        return True
    hay = str(raw or "").lower()
    bucket = normalize_phase(raw).lower()
    for term in terms:
        if term in hay:
            return True
        term_bucket = normalize_phase(term).lower()
        if term_bucket == "other":
            if term.lower() == "other" and bucket == "other":
                return True
        elif term_bucket == bucket:
            return True
    return False


def matching_trial_ids(conn: sqlite3.Connection, rules: Dict[str, Any]) -> List[str]:
    """Compatibility helper for monitor membership evaluation."""
    return sorted({r["source_trial_id"] for r in matching_trials(conn, rules)})

def run_monitor(conn: sqlite3.Connection, monitor_id: int, *, trigger: str = "manual",
                scheduled_for: str | None = None, run_id: int | None = None) -> Dict[str, int]:
    monitor = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    if not monitor: raise ValueError("monitor not found")
    rules_row = conn.execute("SELECT rules_json FROM monitor_rules WHERE monitor_id=?", (monitor_id,)).fetchone()
    rules = json.loads(rules_row[0]) if rules_row else {}
    if run_id is None:
        run_id = conn.execute("INSERT INTO monitor_runs (monitor_id,trigger,scheduled_for) VALUES (?,?,?)",
                              (monitor_id, trigger, scheduled_for)).lastrowid
    try:
        current = set(matching_trial_ids(conn, rules)); previous = {r["trial_id"]: r for r in conn.execute("SELECT * FROM monitor_trials WHERE monitor_id=?", (monitor_id,))}
        initial = not previous; new_count = left_count = 0
        for trial_id in current:
            old = previous.get(trial_id)
            if old is None:
                conn.execute("INSERT INTO monitor_trials (monitor_id,trial_id,last_matched_at,entered_at,last_evaluated_at) VALUES (?,?,datetime('now'),datetime('now'),datetime('now'))", (monitor_id,trial_id))
                # Baseline membership is intentionally quiet.  A monitor is
                # created from an already visible result set, not an alert.
                if not initial:
                    conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?, 'trial_entered')", (monitor_id,trial_id))
                    new_count += 1
            elif not old["currently_matches"]:
                conn.execute("UPDATE monitor_trials SET currently_matches=1, entered_at=datetime('now'), left_at=NULL,last_matched_at=datetime('now'),last_evaluated_at=datetime('now') WHERE id=?", (old["id"],))
                conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_reentered')", (monitor_id,trial_id)); new_count += 1
            else: conn.execute("UPDATE monitor_trials SET last_matched_at=datetime('now'),last_evaluated_at=datetime('now') WHERE id=?", (old["id"],))
        for trial_id, old in previous.items():
            if old["currently_matches"] and trial_id not in current:
                conn.execute("UPDATE monitor_trials SET currently_matches=0,left_at=datetime('now'),last_evaluated_at=datetime('now') WHERE id=?", (old["id"],))
                conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type) VALUES (?,?,'trial_left')", (monitor_id,trial_id)); left_count += 1
        # Existing canonical change events are referenced, never copied/diffed.
        changed = 0
        for trial_id in (() if initial else current):
            events = conn.execute("""SELECT te.event_id FROM trial_events te JOIN registry_records r ON r.record_id=te.record_id
              WHERE r.source_trial_id=? AND te.detected_at > COALESCE((SELECT MAX(completed_at) FROM monitor_runs WHERE monitor_id=? AND id<>? AND status='completed'),'1970-01-01')""", (trial_id,monitor_id,run_id)).fetchall()
            for ev in events:
                conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,related_change_event_id) VALUES (?,?,'trial_changed',?)", (monitor_id,trial_id,ev[0])); changed += 1
        conn.execute("UPDATE monitor_runs SET completed_at=datetime('now'),status='completed',matched_count=?,new_count=?,changed_count=?,left_count=? WHERE id=?", (len(current),new_count,changed,left_count,run_id))
        conn.execute("UPDATE monitors SET last_checked_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (monitor_id,))
        if trigger == "scheduled": conn.execute("UPDATE monitors SET last_scheduled_run_at=? WHERE id=?", (scheduled_for, monitor_id))
        conn.commit()
        # Outbox failure must never erase correctly persisted monitor activity;
        # reconciliation can safely retry because notification identity is durable.
        try:
            from core.notifications import reconcile_notifications
            reconcile_notifications(conn, monitor_id)
        except Exception:
            logger.exception("Notification reconciliation failed after monitor %s; activity remains recoverable", monitor_id)
        return {"matched_count":len(current),"new_count":new_count,"changed_count":changed,"left_count":left_count}
    except Exception as exc:
        conn.execute("UPDATE monitor_runs SET completed_at=datetime('now'),status='failed',error_message=? WHERE id=?", (str(exc),run_id)); conn.commit(); raise
