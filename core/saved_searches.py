"""Passive saved-search storage; never evaluates or schedules a monitor."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

SEARCH_SCHEMA_VERSION = 1
FILTERS = ("q", "registries", "statuses", "study_types", "phase", "country",
           "sponsor", "condition", "intervention")
SORTS = {"last_updated", "start_date", "id"}
PHASES = {"Phase 1", "Phase 1/Phase 2", "Phase 2", "Phase 2/Phase 3",
          "Phase 3", "Phase 3/Phase 4", "Phase 4", "N/A"}
STATUSES = {"Recruiting", "Not yet recruiting", "Active, not recruiting",
            "Enrolling by invitation", "Completed", "Terminated", "Suspended", "Withdrawn"}
STUDY_TYPES = {"Interventional", "Observational"}
REGISTRIES = {"NCT", "ChiCTR", "CTR", "ICTRP", "CTIS", "ISRCTN", "EUCTR"}


def validate_state(raw: Any, version: Any = SEARCH_SCHEMA_VERSION) -> dict[str, Any]:
    """Validate persisted search semantics against the current executable API.

    Page number is deliberately not persisted: reopening starts at page one.
    Empty fields are omitted, matching the compact live-search rule.
    """
    if type(version) is not int or version != SEARCH_SCHEMA_VERSION:
        raise ValueError("unsupported search schema version")
    if not isinstance(raw, dict) or set(raw) - set(FILTERS) - {"sort"}:
        raise ValueError("unknown search field")
    state: dict[str, Any] = {}
    q = raw.get("q", "")
    if not isinstance(q, str) or len(q) > 500:
        raise ValueError("invalid query")
    if q.strip():
        state["q"] = q.strip()
    for key in FILTERS[1:]:
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 20:
            raise ValueError(f"invalid {key}")
        clean = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"invalid {key} value")
            value = value.strip()
            allowed = {"registries": REGISTRIES, "statuses": STATUSES,
                       "study_types": STUDY_TYPES, "phase": PHASES}.get(key)
            if allowed is not None and value not in allowed:
                raise ValueError(f"unknown {key} value")
            if value not in clean:
                clean.append(value)
        if clean:
            state[key] = clean
    sort = raw.get("sort", "last_updated")
    if not isinstance(sort, str) or sort not in SORTS:
        raise ValueError("invalid sort")
    state["sort"] = sort
    if not any(key in state for key in FILTERS):
        raise ValueError("a saved search needs at least one filter")
    return state


def validate_metadata(raw: Any, *, create: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid saved search")
    allowed = {"name", "description", "state", "search_schema_version",
               "original_nl_query", "interpreter_version", "pinned"}
    if set(raw) - allowed or (create and ("name" not in raw or "state" not in raw)):
        raise ValueError("invalid saved search fields")
    out: dict[str, Any] = {}
    if "name" in raw:
        if not isinstance(raw["name"], str) or not 1 <= len(raw["name"].strip()) <= 120:
            raise ValueError("invalid name")
        out["name"] = raw["name"].strip()
    if "description" in raw:
        value = raw["description"]
        if value is not None and (not isinstance(value, str) or len(value) > 1000):
            raise ValueError("invalid description")
        out["description"] = value
    if "state" in raw or "search_schema_version" in raw:
        if "state" not in raw:
            raise ValueError("state is required with schema version")
        out["state"] = validate_state(raw["state"], raw.get("search_schema_version", 1))
    if "original_nl_query" in raw:
        value = raw["original_nl_query"]
        if value is not None and (not isinstance(value, str) or len(value) > 500):
            raise ValueError("invalid original query")
        out["original_nl_query"] = value
    if "interpreter_version" in raw:
        value = raw["interpreter_version"]
        if value is not None and (not isinstance(value, str) or len(value) > 80):
            raise ValueError("invalid interpreter version")
        out["interpreter_version"] = value
    if "pinned" in raw:
        if type(raw["pinned"]) is not bool:
            raise ValueError("invalid pinned state")
        out["pinned"] = int(raw["pinned"])
    if not out:
        raise ValueError("empty update")
    return out


def row_to_saved_search(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    if result["search_schema_version"] != SEARCH_SCHEMA_VERSION:
        raise ValueError("unsupported search schema version")
    result["state"] = validate_state(json.loads(result.pop("state_json")), result["search_schema_version"])
    result["pinned"] = bool(result["pinned"])
    return result
