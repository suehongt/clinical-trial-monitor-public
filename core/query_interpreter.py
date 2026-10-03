"""Conservative, explainable natural-language to monitor-rule proposal.

Only this module interprets language.  It never reads trial records or runs a
monitor.  Search/monitor execution remains in ``core.monitors``.
"""
from __future__ import annotations

import re
from typing import Any

from core.monitors import SUPPORTED_RULES

VERSION = "deterministic-v1"
REGISTRIES = {"nct": "NCT", "clinicaltrials.gov": "NCT", "chictr": "ChiCTR", "ctr": "CTR", "ictrp": "ICTRP", "ctis": "CTIS", "isrctn": "ISRCTN", "euctr": "EUCTR", "eudract": "EUCTR"}
STATUSES = {"recruiting": "Recruiting", "not yet recruiting": "Not yet recruiting", "active, not recruiting": "Active, not recruiting", "completed": "Completed"}
STUDY_TYPES = {"interventional": "Interventional", "observational": "Observational"}
PHASES = {"Phase 1", "Phase 1/Phase 2", "Phase 2", "Phase 2/Phase 3", "Phase 3", "Phase 3/Phase 4", "Phase 4"}
COUNTRIES = {"china": "China", "prc": "China", "中国": "China", "美国": "United States", "united states": "United States", "usa": "United States", "英国": "United Kingdom", "united kingdom": "United Kingdom"}


def validate_proposal(rule: Any) -> dict[str, list[str]]:
    """Validate even provider-generated proposals before they reach a matcher."""
    if not isinstance(rule, dict) or set(rule) - SUPPORTED_RULES:
        raise ValueError("unknown search rule field")
    out: dict[str, list[str]] = {}
    canonical = {"registries": set(REGISTRIES.values()), "statuses": set(STATUSES.values()),
                 "study_types": set(STUDY_TYPES.values()), "phase": PHASES,
                 "country": set(COUNTRIES.values())}
    for key, values in rule.items():
        if not isinstance(values, list) or not values or len(values) > 20:
            raise ValueError(f"invalid {key} values")
        clean = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"invalid {key} value")
            value = value.strip()
            if key in canonical and value not in canonical[key]:
                raise ValueError(f"unknown {key} value: {value}")
            if value not in clean:
                clean.append(value)
        out[key] = clean
    return out


def interpret(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("query must contain 1–500 characters")
    original = text
    remaining = text.strip()
    rule: dict[str, list[str]] = {}
    explanations: list[str] = []
    ambiguities: list[dict[str, str]] = []
    unsupported: list[str] = []

    def take(pattern: str, key: str | None = None, value: str | None = None,
             note: str | None = None) -> bool:
        nonlocal remaining
        match = re.search(pattern, remaining, re.I)
        if not match:
            return False
        remaining = remaining[:match.start()] + " " + remaining[match.end():]
        if key and value:
            rule.setdefault(key, [])
            if value not in rule[key]:
                rule[key].append(value)
        if note:
            explanations.append(note)
        return True

    # Longer/more specific phrases precede shorter aliases.
    for pattern, value in [
        (r"\bclinicaltrials\.gov\b|\bNCT\b", "NCT"),
        (r"\bChiCTR\b", "ChiCTR"), (r"\bICTRP\b", "ICTRP"),
        (r"\bCTIS\b", "CTIS"), (r"\bISRCTN\b", "ISRCTN"),
        (r"\bEUCTR\b|\bEudraCT\b", "EUCTR"),
        (r"\bCTR\b", "CTR"),
    ]:
        take(pattern, "registries", value)
    if take(r"\bchinese registry\b|中国注册平台", note="A Chinese registry may refer to ChiCTR or CTR."):
        ambiguities.append({"term": "Chinese registry", "suggestion": "Choose ChiCTR or CTR explicitly."})
    for pattern, value in [
        (r"\bnot yet recruiting\b|尚未招募", "Not yet recruiting"),
        (r"\bactive,? not recruiting\b", "Active, not recruiting"),
        (r"\b(?:currently |still )?recruiting\b|正在招募|招募中|仍在招募", "Recruiting"),
        (r"\bopen for recruitment\b", "Recruiting"),
        (r"\bcompleted\b|已完成", "Completed"),
    ]:
        take(pattern, "statuses", value)
    if take(r"\bactive\b", note="“active” may mean Recruiting or Active, not recruiting."):
        ambiguities.append({"term": "active", "suggestion": "Choose a recruitment status explicitly."})
    if take(r"\bopen\b", note="“open” may describe recruitment or trial availability."):
        ambiguities.append({"term": "open", "suggestion": "Specify Recruiting if recruitment is intended."})
    for pattern, value in [
        (r"\binterventional\b|干预性研究|药物干预", "Interventional"),
        (r"\bobservational\b|观察性研究", "Observational"),
    ]:
        take(pattern, "study_types", value)
    # Combined phases are represented as one canonical stored value.  An
    # explicit OR instead proposes two values; matcher ORs values in a field.
    for pattern, value in [
        (r"\bphase\s*(?:1|i)\s*/\s*(?:2|ii)\b|(?<![IⅡ])(?:I|Ⅰ)\s*/\s*(?:II|Ⅱ)期", "Phase 1/Phase 2"),
        (r"\bphase\s*(?:2|ii)\s*/\s*(?:3|iii)\b|(?:II|Ⅱ)\s*/\s*(?:III|Ⅲ)\s*期", "Phase 2/Phase 3"),
        (r"\bphase\s*(?:3|iii)\s*/\s*(?:4|iv)\b|(?:III|Ⅲ)\s*/\s*(?:IV|Ⅳ)期", "Phase 3/Phase 4"),
    ]:
        take(pattern, "phase", value)
    # English "phase 2 or 3" and "phase II or III" denote alternatives.
    if take(r"\bphase\s*(?:2|ii)\s+or\s+(?:3|iii)\b", note="Phase 2 or Phase 3 (including combined-phase records)."):
        rule.setdefault("phase", []).extend(["Phase 2", "Phase 3"])
    if "Phase 2/Phase 3" in rule.get("phase", []):
        rule["phase"] = ["Phase 2", "Phase 3"]
        explanations.append("II/III is treated as Phase 2 or Phase 3, including combined-phase records.")
    for number, roman in [(1, "I"), (2, "II"), (3, "III"), (4, "IV")]:
        take(rf"\bphase\s*(?:{number}|{roman})\b|(?<![A-Za-z]){roman}期", "phase", f"Phase {number}")
    for pattern, value in [
        (r"\b(?:in|within)\s+(?:the\s+)?(?:china|prc)\b|中国(?:开展的|的|临床)?|在中国", "China"),
        (r"\b(?:in|within)\s+(?:the\s+)?(?:united states|usa)\b|美国", "United States"),
        (r"\b(?:in|within)\s+(?:the\s+)?united kingdom\b|英国", "United Kingdom"),
    ]:
        if take(pattern, "country", value) and value == "China":
            explanations.append("China is a country filter, not the ChiCTR registry.")
    if take(r"\b(?:in )?europe\b|欧洲", note="Europe is a region, not a supported country rule."):
        unsupported.append("Europe (regional country grouping is unavailable)")
    for pattern in [r"\b(?:updated )?(?:in )?(?:the )?(?:last|past)\s+\d+\s+(?:hours?|days?|months?|years?)\b",
                    r"过去\s*(?:半|一|二|三|六|十|\d+)\s*(?:天|个月|年|小时)", r"最近(?:一个月|\d+天)",
                    r"\bsince\s+[A-Za-z]+\s+\d{4}\b", r"自\d{4}年\d{1,2}月以来"]:
        match = re.search(pattern, remaining, re.I)
        if match:
            unsupported.append(match.group().strip() + " (update-date filtering is unavailable)")
            remaining = remaining[:match.start()] + " " + remaining[match.end():]
    if take(r"\brecent\b|近期|最近", note="Recent has no fixed time window."):
        ambiguities.append({"term": "recent", "suggestion": "Specify a time window; update-date filtering is currently unavailable."})
    for pattern, label in [(r"\bmost promising\b|\bpromising\b|最有前景", "promising"),
                           (r"\bhigh quality\b|高质量", "high quality"),
                           (r"\bbest\b|最佳", "best"),
                           (r"\bbreakthrough\b|突破性", "breakthrough"),
                           (r"\blikely to succeed\b|成功率高", "likely to succeed")]:
        if take(pattern):
            unsupported.append(label + " (no deterministic filter or ranking)")
    # Explicit field intent only.  General disease/topic language stays in
    # the broad query field; no hidden ontology expansion is performed.
    for pattern, field in [(r"\b(?:sponsored by|sponsor:)\s+([A-Za-z][A-Za-z0-9 .&-]+?)(?=\s+(?:trials?|studies?|in|phase|with)\b|$)", "sponsor"),
                           (r"\bcondition:\s*([A-Za-z][A-Za-z0-9 -]+?)(?=\s+(?:trials?|studies?|in)\b|$)", "condition"),
                           (r"\bintervention:\s*([A-Za-z][A-Za-z0-9 -]+?)(?=\s+(?:trials?|studies?|in)\b|$)", "intervention")]:
        match = re.search(pattern, remaining, re.I)
        if match:
            rule[field] = [match.group(1).strip()]
            remaining = remaining[:match.start()] + " " + remaining[match.end():]
    remaining = re.sub(r"\b(?:clinical|trials?|studies?|study|for|of|the|with|only|updated|in|on|about)\b|临床试验|试验|研究|开展的|的|仍在", " ", remaining, flags=re.I)
    remaining = re.sub(r"[\s,，、。;；]+", " ", remaining).strip()
    if remaining:
        rule["query"] = [remaining]
    rule = validate_proposal(rule)
    return {"original_query": original, "rule": rule, "version": VERSION,
            "provider": "deterministic", "confidence": "low" if ambiguities or unsupported else "high",
            "ambiguities": ambiguities, "unsupported": unsupported, "explanations": explanations}
