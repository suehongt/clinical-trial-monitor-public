"""Change severity classification — the ONE central rules module.

Directive §11: severity assignment must be deterministic rule-driven config,
never AI, never scattered across components.  Every caller (pipeline save,
schema backfill, report/export shaping) resolves severity through
``classify_change`` so the classification vocabulary lives here and nowhere
else.

Severity enum (directive): critical > important > normal > minor.
``importance_score`` (0–100) is a stable ranking hint for future sorting.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional

# ── Severity vocabulary ────────────────────────────────────────────────────

CRITICAL = "critical"
IMPORTANT = "important"
NORMAL = "normal"
MINOR = "minor"
SEVERITIES = (CRITICAL, IMPORTANT, NORMAL, MINOR)

# Base score per severity; field-specific rules add a small delta so ranking
# within a band stays stable and explainable.
_BASE_SCORE = {CRITICAL: 90.0, IMPORTANT: 60.0, NORMAL: 30.0, MINOR: 10.0}

# Statuses that end or pause a study — the strongest signal we track.
_TERMINAL_STATUS = re.compile(r"terminat|withdrawn|suspended|中止|终止|暂停", re.I)


@dataclass(frozen=True)
class SeverityResult:
    severity: str
    change_type: str          # modified | status_transition | date_shift | numeric_increase | numeric_decrease | list_item_added | list_item_removed | added | removed
    importance_score: float


# ── Field rules ────────────────────────────────────────────────────────────
# One place mapping tracked field → severity policy.  Fields not listed fall
# back to their change_category (FIELD_CATEGORY in change_detection) via
# _CATEGORY_FALLBACK, then to normal.

_FIELD_RULES = {
    # CRITICAL — study-level redirection
    "primary_endpoint":       (CRITICAL, 5),
    "interventions":          (CRITICAL, 4),
    "arm_group_interventions": (CRITICAL, 4),
    "sponsors":               (CRITICAL, 3),
    # IMPORTANT — material operational change
    "enrollment":             (IMPORTANT, 3),   # 20% rule applied dynamically
    "primary_completion_date": (IMPORTANT, 2),
    "completion_date":        (IMPORTANT, 1),
    "eligibility_criteria":   (IMPORTANT, 0),
    "countries":              (IMPORTANT, 0),
    "locations":              (IMPORTANT, 0),
    "study_design":           (IMPORTANT, 0),
    "study_phase":            (IMPORTANT, 0),
    # NORMAL — worth knowing, not action-driving
    "secondary_endpoints":    (NORMAL, 0),
    "conditions":             (NORMAL, 0),
    "start_date":             (NORMAL, 0),
    "registration_date":      (NORMAL, 0),
    # MINOR — bookkeeping / source noise
    "last_updated_at_source": (MINOR, 0),
    "contacts":               (MINOR, 0),
}

# change_category fallback for fields missing from _FIELD_RULES
_CATEGORY_FALLBACK = {
    "status": IMPORTANT,
    "endpoint": NORMAL,
    "date": NORMAL,
    "location": IMPORTANT,
    "sponsor": CRITICAL,
    "enrollment": IMPORTANT,
    "design": IMPORTANT,
    "other": NORMAL,
}

_DATE_FIELDS = {
    "start_date", "primary_completion_date", "completion_date", "registration_date",
}
_LIST_FIELDS = {
    "conditions", "countries", "locations", "sponsors", "interventions",
    "secondary_endpoints", "arm_group_interventions", "contacts",
}

# Enrollment changes below this relative threshold are downgraded to normal
# (directive: "enrollment changed by >= 20% where numeric comparison is valid").
_ENROLLMENT_MATERIAL_RATIO = 0.20


# ── Helpers ────────────────────────────────────────────────────────────────


def _as_number(v: Any) -> Optional[float]:
    try:
        s = str(v).strip()
        return float(s)
    except (TypeError, ValueError):
        return None


def _as_list(v: Any) -> Optional[List[Any]]:
    if v is None:
        return None
    try:
        parsed = json.loads(v) if isinstance(v, str) else v
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _list_change_type(old: Any, new: Any) -> str:
    """set-style diff for semantically unordered lists (directive §12:
    reordering alone is already suppressed upstream by compare_values)."""
    old_items = {_norm_item(x) for x in (_as_list(old) or [])}
    new_items = {_norm_item(x) for x in (_as_list(new) or [])}
    if new_items - old_items:
        return "list_item_added"
    if old_items - new_items:
        return "list_item_removed"
    return "modified"


def _norm_item(x: Any) -> str:
    if isinstance(x, (dict, list)):
        return json.dumps(x, sort_keys=True, ensure_ascii=False)
    return str(x).strip()


# ── The classifier ─────────────────────────────────────────────────────────


def classify_change(
    field_name: str,
    old_value: Any,
    new_value: Any,
    change_category: str = "",
    old_status_label: Optional[str] = None,
    new_status_label: Optional[str] = None,
) -> SeverityResult:
    """Deterministic severity for one field-level change.

    ``old/new_status_label`` carry the status_types labels for ``status_id``
    events (raw values are integer FKs); any other field ignores them.
    """
    # ── status transitions: terminal > any other transition ────────────
    if field_name == "status_id":
        target = new_status_label or (str(new_value) if new_value is not None else "")
        if _TERMINAL_STATUS.search(target or ""):
            return SeverityResult(CRITICAL, "status_transition", _BASE_SCORE[CRITICAL] + 5)
        return SeverityResult(IMPORTANT, "status_transition", _BASE_SCORE[IMPORTANT])

    # ── enrollment: numeric_increase/decrease with 20% materiality ──────
    if field_name == "enrollment":
        old_n, new_n = _as_number(old_value), _as_number(new_value)
        if old_n is None or new_n is None:
            kind = "added" if new_value not in (None, "") else "removed"
            return SeverityResult(IMPORTANT, kind, _BASE_SCORE[IMPORTANT])
        if old_n != 0:
            change_type = "numeric_increase" if new_n > old_n else "numeric_decrease"
            ratio = abs(new_n - old_n) / abs(old_n)
            if ratio >= _ENROLLMENT_MATERIAL_RATIO:
                return SeverityResult(IMPORTANT, change_type,
                                      _BASE_SCORE[IMPORTANT] + _FIELD_RULES["enrollment"][1])
            return SeverityResult(NORMAL, change_type, _BASE_SCORE[NORMAL])
        return SeverityResult(IMPORTANT, "modified", _BASE_SCORE[IMPORTANT])

    # ── dates: date_shift ───────────────────────────────────────────────
    if field_name in _DATE_FIELDS:
        base, delta = _FIELD_RULES.get(field_name, (NORMAL, 0))
        return SeverityResult(base, "date_shift", _BASE_SCORE[base] + delta)

    # ── set-like lists: added/removed granularity ───────────────────────
    if field_name in _LIST_FIELDS:
        base, delta = _FIELD_RULES.get(field_name, (NORMAL, 0))
        return SeverityResult(base, _list_change_type(old_value, new_value),
                              _BASE_SCORE[base] + delta)

    # ── everything else: table-driven with category fallback ────────────
    if field_name in _FIELD_RULES:
        base, delta = _FIELD_RULES[field_name]
    else:
        base = _CATEGORY_FALLBACK.get(change_category, NORMAL)
        delta = 0
    change_type = "added" if old_value in (None, "") else (
        "removed" if new_value in (None, "") else "modified")
    return SeverityResult(base, change_type, _BASE_SCORE[base] + delta)


def classify_event_row(row: dict, status_labels: Optional[dict] = None) -> SeverityResult:
    """Classify a trial_events-shaped dict (field_name/old_value/new_value/
    change_category).  status_id FK values are resolved through
    ``status_labels`` ({str(id): label}) when provided."""
    old_label = new_label = None
    if row.get("field_name") == "status_id" and status_labels:
        old_label = status_labels.get(str(row.get("old_value")))
        new_label = status_labels.get(str(row.get("new_value")))
    return classify_change(
        row.get("field_name") or "",
        row.get("old_value"),
        row.get("new_value"),
        row.get("change_category") or "",
        old_status_label=old_label,
        new_status_label=new_label,
    )
