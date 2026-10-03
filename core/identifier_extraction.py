"""
Identifier Extraction — find known registry identifiers in trial records.

Extracted identifier types:
  - NCT   (ClinicalTrials.gov)     e.g. NCT12345678
  - CTR   (China Drug Trials)      e.g. CTR20251234
  - ChiCTR (Chinese Clinical Trial Register)  e.g. ChiCTR2000000000
  - CTIS  (EU Clinical Trials Information System) e.g. 2024-500001-12-00
  - ISRCTN (ISRCTN Registry)       e.g. ISRCTN12345678
  - EudraCT (legacy EUCTR)         e.g. 2019-000001-12
  - UTN   (Universal Trial Number) e.g. U1111-1234-5678
  - ProtocolNumber                 e.g. PROTOCOL-2025-001, ALN-HTN01-001, …

Extraction sources (priority order):
  1. source_trial_id  (definitive — the registry's own ID for this record)
  2. Structured fields in raw_payload (secondary IDs, sponsor IDs, etc.)
  3. Free-text search of raw_payload JSON
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from db.connection import get_connection, transaction

logger = logging.getLogger(__name__)

# ── Regex patterns for known identifier formats ──────────────────────────

IDENTIFIER_PATTERNS: dict[str, re.Pattern] = {
    # (?<![A-Za-z]) 左边界：ChiCTR 自带注册号 "ChiCTR2600131888" 此前会被
    # CTR 模式从第 3 个字符截出假号 "CTR26001318"（同百号段记录共享同一假
    # ID → 实体解析批量误合并，见 myocarditis 案例 2026-09-08 取证）。
    # CTR 收紧为 CTR20\d{6}：真号自 2013 年起年份前缀恒为 20xx（CTR20263443），
    # 缩写年伪号（CTR26xxxxxx）直接不再匹配。
    "NCT": re.compile(r'(?<![A-Za-z])(NCT\d{8})', re.IGNORECASE),
    "CTR": re.compile(r'(?<![A-Za-z])(CTR20\d{6})'),
    "ChiCTR": re.compile(r'(?<![A-Za-z])(ChiCTR\d{8,})', re.IGNORECASE),
    "UTN": re.compile(r'(?<![A-Za-z])(U1111-\d{4}-\d{4})'),
    "CTIS": re.compile(r'(?<![A-Za-z])(?<!\d)(20\d{2}-\d{6}-\d{2}-\d{2})(?!\d)'),
    "ISRCTN": re.compile(r'(?<![A-Za-z])(ISRCTN\d{8})(?!\d)', re.IGNORECASE),
    "EudraCT": re.compile(r'(?<![A-Za-z])(?<!\d)(20\d{2}-\d{6}-\d{2})(?!-\d{2}|\d)'),
}

# Loose protocol-number pattern — catches identifiers that look like
# alphanumeric protocol codes (but not other known ID types).
# Typically: uppercase letters + digits + hyphens, 4-40 chars.
_PROTOCOL_RE = re.compile(
    r'(?:(?<=\s)|(?<=^)|(?<=["\'\[\{,:<>\n\r]))'
    r'([A-Z][A-Z0-9]+-\d[\w-]{2,})'
    r'(?=\s|$|["\'\]\}:,<>/])',
)


def get_identifier_type(value: str) -> str:
    """Classify a string into an identifier type."""
    for id_type, pattern in IDENTIFIER_PATTERNS.items():
        if pattern.fullmatch(value.strip()):
            return id_type
    return "ProtocolNumber"


def extract_and_save_identifiers(record_id: int) -> List[Dict[str, Any]]:
    """Extract all identifiers from a registry_record and persist to trial_identifiers.

    Returns the list of extracted identifiers (with dedup).
    """
    conn = get_connection()
    rec = conn.execute(
        """SELECT r.*, src.short_name as source_short_name
           FROM registry_records r
           JOIN registry_sources src ON src.source_id = r.source_id
           WHERE r.record_id = ?""",
        (record_id,),
    ).fetchone()

    if not rec:
        return []

    results: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()  # (type, value)

    # ── 1. source_trial_id (definitive) ──────────────────────────────
    stid = rec["source_trial_id"]
    if stid:
        _add(results, seen,
             identifier_type=get_identifier_type(stid),
             identifier_value=_normalise_id(stid),
             source_field="source_trial_id",
             confidence=1.0)

    # ── 2. Structured fields in raw_payload ──────────────────────────
    raw = rec["raw_payload"]
    if raw:
        try:
            payload = json.loads(raw)
            _extract_from_structured(payload, results, seen)
        except json.JSONDecodeError:
            pass

        # ── 3. Free-text search of raw_payload string ────────────────
        _search_text(raw, results, seen, exclude=stid)

    # ── Persist ──────────────────────────────────────────────────────
    _save_identifiers(conn, record_id, results)

    return results


# ── Internal helpers ─────────────────────────────────────────────────────


def _normalise_id(value: str) -> str:
    """Normalise an identifier to a canonical form."""
    value = value.strip()
    # Registry-prefixed IDs are case-insensitive; store uppercase.
    if value.upper().startswith(("NCT", "ISRCTN")):
        return value.upper()
    # ChiCTR IDs are case-sensitive but store as-is (already canonical)
    return value


def _add(results: list, seen: set, *,
         identifier_type: str, identifier_value: str,
         source_field: str, confidence: float) -> None:
    """Add an identifier if not already seen (dedup by type+value)."""
    key = (identifier_type, identifier_value)
    if key not in seen and identifier_value:
        seen.add(key)
        results.append({
            "identifier_type": identifier_type,
            "identifier_value": identifier_value,
            "source_field": source_field,
            "confidence": confidence,
        })


def _extract_from_structured(payload: dict, results: list, seen: set) -> None:
    """Extract identifiers from known structured paths in the API payload.

    ClinicalTrials.gov v2 structure:
      protocolSection.identificationModule.secondaryIdInfos[].secondaryId

    WHO ICTRP (AGGREGATOR) structure:
      _aggregator._who_source_registry / _who_source_trial_id
      secondary_ids[]
    """
    # ── WHO ICTRP AGGREGATOR extraction ───────────────────────────────
    if "_aggregator" in payload:
        who_info = payload["_aggregator"]
        who_trial_id = who_info.get("_who_source_trial_id", "")
        if who_trial_id:
            _add(results, seen,
                 identifier_type=get_identifier_type(who_trial_id),
                 identifier_value=_normalise_id(who_trial_id),
                 source_field="who_aggregator",
                 confidence=1.0)

    # Secondary IDs from WHO ICTRP
    secondary_ids = payload.get("secondary_ids", [])
    for sid in secondary_ids:
        if sid:
            _add(results, seen,
                 identifier_type=get_identifier_type(sid),
                 identifier_value=_normalise_id(sid),
                 source_field=("who_secondary_id" if "_aggregator" in payload
                               else "secondary_id"),
                 confidence=0.95)

    # Direct CTIS records keep public search/detail payloads together.
    ctis_detail = payload.get("_detail") or {}
    ctis_search = payload.get("_search") or {}
    ctis_number = ctis_detail.get("ctNumber") or ctis_search.get("ctNumber")
    if ctis_number:
        _add(results, seen,
             identifier_type="CTIS", identifier_value=_normalise_id(ctis_number),
             source_field="ctis_number", confidence=1.0)
    registries = (((ctis_detail.get("authorizedApplication") or {})
                   .get("authorizedPartI") or {}).get("trialDetails") or {})
    registries = ((registries.get("clinicalTrialIdentifiers") or {})
                  .get("secondaryIdentifyingNumbers") or {}).get("additionalRegistries") or []
    for item in registries:
        if isinstance(item, dict):
            value = item.get("registryNumber") or item.get("number")
        else:
            value = item
        if value:
            _add(results, seen,
                 identifier_type=get_identifier_type(str(value)),
                 identifier_value=_normalise_id(str(value)),
                 source_field="ctis_secondary_id", confidence=0.95)

    # ── ClinicalTrials.gov v2 extraction ──────────────────────────────
    ps = payload.get("protocolSection") or payload
    if ps is not None and isinstance(ps, dict):
        id_mod = ps.get("identificationModule") or {}

        # Secondary IDs (NCT, protocol numbers, etc.)
        secondary_infos = id_mod.get("secondaryIdInfos") or []
        for info in secondary_infos:
            sid = info.get("secondaryId") or info.get("id") or ""
            if sid:
                _add(results, seen,
                     identifier_type=get_identifier_type(sid),
                     identifier_value=_normalise_id(sid),
                     source_field="secondary_id",
                     confidence=0.95)

        # Sponsor / grant numbers
        spon_mod = ps.get("sponsorCollaboratorsModule") or {}
        lead = spon_mod.get("leadSponsor") or {}
        if lead.get("name"):
            _add(results, seen,
                 identifier_type="ProtocolNumber",
                 identifier_value=_normalise_id(lead["name"]),
                 source_field="lead_sponsor",
                 confidence=0.7)


def _search_text(raw_text: str, results: list, seen: set,
                 exclude: Optional[str] = None) -> None:
    """Search raw JSON text for identifier patterns.

    ``exclude`` is the record's own source_trial_id (already extracted, skip dupe).
    """
    for id_type, pattern in IDENTIFIER_PATTERNS.items():
        for match in pattern.finditer(raw_text):
            value = _normalise_id(match.group(1))
            if value == exclude:
                continue
            _add(results, seen,
                 identifier_type=id_type,
                 identifier_value=value,
                 source_field="raw_payload",
                 confidence=0.8)

    # Protocol numbers (only if we found none via structured paths)
    has_protocol = any(r["identifier_type"] == "ProtocolNumber" for r in results)
    if not has_protocol:
        for match in _PROTOCOL_RE.finditer(raw_text):
            value = match.group(1).strip()
            if value == exclude:
                continue
            # Skip purely numeric strings (not a protocol number)
            if value and value.isdigit():
                continue
            _add(results, seen,
                 identifier_type="ProtocolNumber",
                 identifier_value=value,
                 source_field="raw_payload",
                 confidence=0.6)
            break  # take the first plausible protocol number only


def _save_identifiers(conn, record_id: int, identifiers: List[Dict]) -> None:
    """Persist identifiers to the trial_identifiers table (idempotent)."""
    with transaction():
        for idf in identifiers:
            conn.execute(
                """INSERT OR IGNORE INTO trial_identifiers
                   (record_id, identifier_type, identifier_value, source_field, confidence)
                   VALUES (?, ?, ?, ?, ?)""",
                (record_id, idf["identifier_type"], idf["identifier_value"],
                 idf["source_field"], idf["confidence"]),
            )
