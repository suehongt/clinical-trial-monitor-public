"""EU Clinical Trials Register historical protocol backfill collector.

EUCTR is the legacy register for trials authorised under the former EU
Clinical Trials Directive. New and transitioned trials belong in CTIS; this
adapter is deliberately manual/backfill-only and stores one preferred country
protocol per EudraCT number while retaining the complete source text.
"""
from __future__ import annotations

import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests

from collectors.base import BaseCollector, NormalisedRecord, or_terms, to_json_array
from collectors.http import RequestPacer

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"20\d{2}-\d{6}-\d{2}")
_PROTOCOL_RE = re.compile(r"\b([A-Z]{2}|3rd)\(([^)]*)\)", re.IGNORECASE)

_COUNTRIES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia",
    "CY": "Cyprus", "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia",
    "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece",
    "HU": "Hungary", "IS": "Iceland", "IE": "Ireland", "IT": "Italy",
    "LV": "Latvia", "LI": "Liechtenstein", "LT": "Lithuania",
    "LU": "Luxembourg", "MT": "Malta", "NL": "Netherlands",
    "NO": "Norway", "PL": "Poland", "PT": "Portugal", "RO": "Romania",
    "SK": "Slovakia", "SI": "Slovenia", "ES": "Spain", "SE": "Sweden",
    "GB": "United Kingdom", "XI": "Northern Ireland", "3RD": "Outside EEA",
}


def _date(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else None


def _status(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    key = text.casefold()
    if not key:
        return None
    if "premature" in key or "terminated" in key:
        return "Terminated"
    if "suspend" in key or "temporarily halt" in key:
        return "Suspended"
    if "withdraw" in key or "not authorised" in key or "not authorized" in key:
        return "Withdrawn"
    if "complete" in key or "ended" in key:
        return "Completed"
    if "ongoing" in key:
        return "Active, not recruiting"
    if "transition" in key or "authoris" in key or "authoriz" in key:
        return "Registered"
    return text


def _unique(values: List[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in result:
            result.append(text)
    return result


def _parse_summary(text: str) -> List[Dict[str, Any]]:
    """Parse EUCTR's stable plain-text, 20-record summary format."""
    records: List[Dict[str, Any]] = []
    blocks = re.split(r"\n(?=EudraCT Number:\s*)", text.strip())
    for block in blocks:
        match = re.search(r"^EudraCT Number:\s*(20\d{2}-\d{6}-\d{2})", block, re.M)
        if not match:
            continue
        fields: Dict[str, List[str]] = {}
        for line in block.splitlines():
            field = re.match(r"^([^:]+):\s*(.*)$", line.strip())
            if field:
                fields.setdefault(field.group(1).strip(), []).append(field.group(2).strip())
        protocol_text = " ".join(fields.get("Trial protocol", []))
        protocols = [
            {"country_code": code.upper(), "status": status.strip()}
            for code, status in _PROTOCOL_RE.findall(protocol_text)
        ]
        records.append({
            "source_trial_id": match.group(1),
            "title": (fields.get("Full Title") or [None])[0],
            "sponsor_protocol": (fields.get("Sponsor Protocol Number") or [None])[0],
            "sponsor": (fields.get("Sponsor Name") or [None])[0],
            "start_date": (fields.get("Start Date") or [None])[0],
            "medical_condition": (fields.get("Medical condition") or [None])[0],
            "diseases": fields.get("Disease", []),
            "protocols": protocols,
            "source_url": (fields.get("Link") or [None])[0],
            "_summary_text": block,
        })
    return records


def _parse_fields(text: str) -> Dict[str, List[str]]:
    """Parse the colon-delimited protocol text without discarding repeats."""
    fields: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for raw_line in text.replace("\r", "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:]{1,220}):\s*(.*)$", line)
        if match:
            key = re.sub(r"\s+", " ", match.group(1).strip())
            fields.setdefault(key, []).append(match.group(2).strip())
            current = key
        elif re.match(r"^(?:[A-Z]\.|Sponsor \d+|MedDRA Classification$)", line):
            current = None
        elif current:
            fields[current][-1] = (fields[current][-1] + "\n" + line).strip()
    return fields


def _matching_values(fields: Dict[str, List[str]], *needles: str) -> List[str]:
    values: List[str] = []
    for key, entries in fields.items():
        low = key.casefold()
        if all(needle.casefold() in low for needle in needles):
            values.extend(entries)
    return _unique(values)


def _first(fields: Dict[str, List[str]], *needles: str) -> Optional[str]:
    values = _matching_values(fields, *needles)
    return values[0] if values else None


def _preferred_country(protocols: List[Dict[str, str]]) -> Optional[str]:
    for protocol in protocols:
        code = protocol.get("country_code", "").upper()
        if code not in {"GB", "3RD"}:
            return code
    return protocols[0].get("country_code") if protocols else None


class EUCTRCollector(BaseCollector):
    """Manual historical backfill collector for the legacy EUCTR register."""

    allow_empty_response = True

    def __init__(self):
        super().__init__("euctr")

    def fetch_new_or_updated(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        if since:
            logger.info("EUCTR has no reliable updated-since filter; running bounded historical backfill")
        base = self.cfg.api_base.rstrip("/")
        # EUCTR's parser ANDs bare words (the NCT-style OR string scored
        # 687 vs 968 for "myocardial infarction" alone); quoted phrases make
        # OR union correctly (verified live: 968 + 219 → 1035 union).
        query = " OR ".join(f'"{t}"' for t in or_terms((self.cfg.extra or {}).get("query_cond") or ""))
        params = {"query": query}
        pacer = RequestPacer(self.cfg.request_delay_sec)
        detail_workers = max(1, min(8, int(
            (self.cfg.extra or {}).get("detail_workers", 4))))
        pacer.wait()
        response = requests.get(f"{base}/ctr-search/search", params=params,
                                timeout=self.cfg.timeout_sec)
        response.raise_for_status()
        count_match = re.search(
            r"Trials with a EudraCT protocol\s*\(([0-9,.]+)\)", response.text)
        if not count_match:
            raise RuntimeError("EUCTR search response did not contain a trial count")
        total = int(re.sub(r"[,.]", "", count_match.group(1)))
        limit = self.cfg.max_records_per_run or total
        wanted = min(total, limit)
        if wanted == 0:
            return []

        summaries: List[Dict[str, Any]] = []
        for page in range(1, math.ceil(wanted / 20) + 1):
            pacer.wait()
            summary_response = requests.get(
                f"{base}/ctr-search/rest/download/summary",
                params={"query": query, "page": page, "mode": "current_page"},
                timeout=self.cfg.timeout_sec,
            )
            summary_response.raise_for_status()
            summaries.extend(_parse_summary(summary_response.text))
            if len(summaries) >= wanted:
                break

        def fetch_detail(summary: Dict[str, Any]) -> Dict[str, Any]:
            country = _preferred_country(summary.get("protocols") or [])
            detail_text = ""
            if country:
                try:
                    pacer.wait()
                    detail_response = requests.get(
                        f"{base}/ctr-search/rest/download/trial/"
                        f"{summary['source_trial_id']}/{country}",
                        timeout=self.cfg.timeout_sec,
                    )
                    detail_response.raise_for_status()
                    detail_text = detail_response.text
                except requests.RequestException as exc:
                    logger.warning("EUCTR detail failed for %s/%s; keeping summary: %s",
                                   summary["source_trial_id"], country, exc)
            return {
                **summary,
                "selected_country_code": country,
                "fields": _parse_fields(detail_text) if detail_text else {},
                "_detail_text": detail_text,
            }

        with ThreadPoolExecutor(max_workers=detail_workers) as executor:
            return list(executor.map(fetch_detail, summaries[:wanted]))

    def normalise(self, raw: Dict[str, Any]) -> NormalisedRecord:
        fields = raw.get("fields") or {}
        trial_id = raw.get("source_trial_id")
        if not trial_id or not _ID_RE.fullmatch(str(trial_id)):
            raise ValueError("EUCTR record is missing a valid EudraCT number")

        title = (_first(fields, "full title of the trial") or raw.get("title")
                 or str(trial_id))
        conditions = _unique(
            _matching_values(fields, "medical condition(s) being investigated")
            + _matching_values(fields, "e.1.2 term")
            + ([raw.get("medical_condition")] if raw.get("medical_condition") else [])
        )
        interventions = _unique(
            _matching_values(fields, "trade name")
            + _matching_values(fields, "inn - proposed inn")
            + _matching_values(fields, "other descriptive name")
        )
        sponsors = _matching_values(fields, "name of sponsor")
        if not sponsors and raw.get("sponsor"):
            sponsors = [raw["sponsor"]]

        protocols = raw.get("protocols") or []
        countries = _unique([
            _COUNTRIES.get(str(p.get("country_code", "")).upper(),
                           str(p.get("country_code", "")).upper())
            for p in protocols if p.get("country_code")
        ])
        if not countries:
            member_state = _first(fields, "member state concerned")
            if member_state:
                countries = [member_state.split(" - ", 1)[0].strip()]

        status_value = _first(fields, "trial status")
        if not status_value and protocols:
            selected = raw.get("selected_country_code")
            status_value = next((p.get("status") for p in protocols
                                 if p.get("country_code") == selected), None)

        phases = []
        for needle, label in (("e.7.1 human pharmacology", "Phase 1"),
                              ("e.7.2 therapeutic exploratory", "Phase 2"),
                              ("e.7.3 therapeutic confirmatory", "Phase 3"),
                              ("e.7.4 therapeutic use", "Phase 4")):
            if (_first(fields, needle) or "").casefold() == "yes":
                phases.append(label)

        design = []
        for needle, label in (("e.8.1 controlled", "Controlled"),
                              ("e.8.1.1 randomised", "Randomized"),
                              ("e.8.1.2 open", "Open"),
                              ("e.8.1.3 single blind", "Single blind"),
                              ("e.8.1.4 double blind", "Double blind"),
                              ("e.8.1.5 parallel group", "Parallel"),
                              ("e.8.1.6 cross over", "Crossover")):
            if (_first(fields, needle) or "").casefold() == "yes":
                design.append(label)

        enrollment_text = (_first(fields, "f.4.2.2", "whole clinical trial")
                           or _first(fields, "f.4.2.1", "eea")
                           or _first(fields, "f.4.1", "member state"))
        enrollment_match = re.search(r"\d[\d,]*", enrollment_text or "")
        enrollment = int(enrollment_match.group(0).replace(",", "")) if enrollment_match else None

        inclusion = _first(fields, "principal inclusion criteria")
        exclusion = _first(fields, "principal exclusion criteria")
        eligibility = None
        if inclusion or exclusion:
            eligibility = ("Inclusion:\n" + (inclusion or "")
                           + "\n\nExclusion:\n" + (exclusion or ""))

        secondary_ids = _unique(
            [raw.get("sponsor_protocol"), _first(fields, "sponsor's protocol code number")]
        )
        raw_payload = {**raw, "secondary_ids": secondary_ids}
        country = raw.get("selected_country_code")
        source_url = (f"{self.cfg.base_url.rstrip('/')}/ctr-search/trial/"
                      f"{trial_id}/{country}/" if country else raw.get("source_url"))
        return NormalisedRecord(
            source_trial_id=str(trial_id),
            title=str(title),
            scientific_title=str(title),
            study_type="Interventional",
            status=_status(status_value),
            enrollment=enrollment,
            registration_date=_date(_first(fields, "first entered", "eudract database")),
            start_date=_date(raw.get("start_date")),
            completion_date=_date(_first(fields, "date of the global end of the trial")),
            conditions=to_json_array(conditions),
            interventions=to_json_array(interventions),
            countries=to_json_array(countries),
            sponsors=to_json_array(sponsors),
            study_phase="/".join(phases) if phases else None,
            study_design="; ".join(design) if design else None,
            eligibility_criteria=eligibility,
            primary_endpoint=_first(fields, "primary end point"),
            secondary_endpoints=to_json_array(_matching_values(fields, "secondary end point")),
            source_url=source_url,
            raw_payload=json.dumps(raw_payload, ensure_ascii=False, default=str),
        )
