"""ISRCTN public XML API collector."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

from collectors.base import BaseCollector, NormalisedRecord, or_terms, to_json_array


def _text(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    found = node.find(path)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _texts(node: Optional[ET.Element], path: str) -> List[str]:
    if node is None:
        return []
    out: List[str] = []
    for found in node.findall(path):
        value = (found.text or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else None


def _stamp(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        # ISRCTN emits nanoseconds; Python's ISO parser accepts microseconds.
        cleaned = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d|$)", r"\1", value)
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _status(raw: Dict[str, Any]) -> str:
    explicit = (raw.get("recruitment_status") or "").strip().casefold()
    if explicit:
        if "not yet" in explicit or "not started" in explicit:
            return "Not yet recruiting"
        if "recruit" in explicit:
            return "Recruiting"
        if "complete" in explicit or "closed" in explicit:
            return "Completed"
        if "suspend" in explicit:
            return "Suspended"
        if "withdraw" in explicit:
            return "Withdrawn"
    today = datetime.now(timezone.utc).date()
    start = _date(raw.get("start_date"))
    recruitment_end = _date(raw.get("recruitment_end"))
    completion = _date(raw.get("completion_date"))
    if start and today < datetime.fromisoformat(start).date():
        return "Not yet recruiting"
    if completion and today > datetime.fromisoformat(completion).date():
        return "Completed"
    if recruitment_end and today > datetime.fromisoformat(recruitment_end).date():
        return "Active, not recruiting"
    return "Recruiting" if start else "Registered"


def _parse_full_trial(full: ET.Element) -> Optional[Dict[str, Any]]:
    trial = full.find("{*}trial")
    if trial is None:
        return None
    number = trial.get("publicIdentifierCanonical") or _text(trial, "{*}isrctn")
    if not number:
        return None
    if not number.upper().startswith("ISRCTN"):
        number = "ISRCTN" + number
    interventions = []
    phases = []
    for intervention in trial.findall("{*}interventions/{*}intervention"):
        name = _text(intervention, "{*}drugNames") or _text(intervention, "{*}description")
        if name and name not in interventions:
            interventions.append(name)
        phase = _text(intervention, "{*}phase")
        if phase and phase not in phases:
            phases.append(phase)
    locations = []
    for centre in trial.findall("{*}participants/{*}trialCentres/{*}trialCentre"):
        locations.append({
            "facility": _text(centre, "{*}name"),
            "city": _text(centre, "{*}city"),
            "state": _text(centre, "{*}state"),
            "country": _text(centre, "{*}country"),
            "postal_code": _text(centre, "{*}zip"),
        })
    design = []
    for path in ("{*}trialDesign/{*}primaryStudyDesign", "{*}trialDesign/{*}interventionalTrialDesign/{*}allocation",
                 "{*}trialDesign/{*}interventionalTrialDesign/{*}masking", "{*}trialDesign/{*}interventionalTrialDesign/{*}control",
                 "{*}trialDesign/{*}interventionalTrialDesign/{*}assignment"):
        value = _text(trial, path)
        if value:
            design.append(value)
    secondary_ids = _texts(trial, "{*}externalRefs/{*}secondaryNumbers/{*}secondaryNumber")
    for path in ("{*}externalRefs/{*}clinicalTrialsGovNumber", "{*}externalRefs/{*}eudraCTNumber"):
        value = _text(trial, path)
        if value and value not in secondary_ids:
            secondary_ids.append(value)
    raw = {
        "source_trial_id": number.upper(),
        "title": _text(trial, "{*}trialDescription/{*}title"),
        "scientific_title": _text(trial, "{*}trialDescription/{*}scientificTitle"),
        "study_type": _text(trial, "{*}trialDesign/{*}primaryStudyDesign"),
        "last_updated": trial.get("lastUpdated"),
        "registration_date": trial.get("publicIdentifierDateAssigned") or (trial.find("{*}isrctn").get("dateAssigned") if trial.find("{*}isrctn") is not None else None),
        "start_date": _text(trial, "{*}participants/{*}recruitmentStart"),
        "recruitment_end": _text(trial, "{*}participants/{*}recruitmentEnd"),
        "completion_date": _text(trial, "{*}trialDesign/{*}overallEndDate"),
        "recruitment_status": _text(trial, "{*}participants/{*}recruitmentStatusOverride") or _text(trial, "{*}participants/{*}recruitmentStartStatusOverride"),
        "enrollment": _text(trial, "{*}participants/{*}targetEnrolment"),
        "conditions": _texts(trial, "{*}conditions/{*}condition/{*}description"),
        "interventions": interventions,
        "countries": _texts(trial, "{*}participants/{*}recruitmentCountries/{*}country"),
        "locations": locations,
        "sponsors": _texts(full, "{*}sponsor/{*}organisation"),
        "funders": _texts(full, "{*}funder/{*}name"),
        "phases": phases,
        "study_design": "; ".join(design) or None,
        "inclusion": _text(trial, "{*}participants/{*}inclusion"),
        "exclusion": _text(trial, "{*}participants/{*}exclusion"),
        "primary_endpoint": _text(trial, "{*}trialDescription/{*}primaryOutcome"),
        "secondary_endpoints": _texts(trial, "{*}trialDescription/{*}secondaryOutcome"),
        "secondary_ids": secondary_ids,
        "_raw_xml": ET.tostring(full, encoding="unicode"),
    }
    return raw


class ISRCTNCollector(BaseCollector):
    allow_empty_response = True

    def __init__(self):
        super().__init__("isrctn")

    def fetch_new_or_updated(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        # The ISRCTN query parser treats a bare multi-word term as AND-ed
        # keywords ("a b OR c" scored 214 vs 1542 for "a b" alone), so each
        # term must be a quoted phrase for OR to union correctly.
        query = " OR ".join(f'"{t}"' for t in or_terms((self.cfg.extra or {}).get("query_cond") or ""))
        if since:
            edited = f"lastEdited GE {since.replace(' ', 'T')}"
            query = f"({query}) AND {edited}" if query else edited
        params = {"limit": self.cfg.max_records_per_run or 1000, "q": query}
        response = requests.get(f"{self.cfg.api_base.rstrip('/')}/api/query/format/default", params=params, timeout=self.cfg.timeout_sec)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        records = []
        for full in root.findall("{*}fullTrial"):
            parsed = _parse_full_trial(full)
            if parsed:
                records.append(parsed)
        return records

    def normalise(self, raw: Dict[str, Any]) -> NormalisedRecord:
        try:
            enrollment = int(raw["enrollment"]) if raw.get("enrollment") else None
        except (TypeError, ValueError):
            enrollment = None
        eligibility = None
        if raw.get("inclusion") or raw.get("exclusion"):
            eligibility = "Inclusion:\n" + (raw.get("inclusion") or "") + "\n\nExclusion:\n" + (raw.get("exclusion") or "")
        sponsors = list(raw.get("sponsors") or [])
        for funder in raw.get("funders") or []:
            if funder not in sponsors:
                sponsors.append(funder)
        return NormalisedRecord(
            source_trial_id=raw["source_trial_id"],
            title=raw.get("title") or raw["source_trial_id"],
            scientific_title=raw.get("scientific_title"),
            study_type=raw.get("study_type"),
            status=_status(raw),
            enrollment=enrollment,
            registration_date=_date(raw.get("registration_date")),
            start_date=_date(raw.get("start_date")),
            primary_completion_date=_date(raw.get("recruitment_end")),
            completion_date=_date(raw.get("completion_date")),
            last_updated_at_source=_stamp(raw.get("last_updated")),
            conditions=to_json_array(raw.get("conditions")),
            interventions=to_json_array(raw.get("interventions")),
            countries=to_json_array(raw.get("countries")),
            locations=json.dumps(raw.get("locations"), ensure_ascii=False) if raw.get("locations") else None,
            sponsors=to_json_array(sponsors),
            study_phase="/".join(raw.get("phases") or []) or None,
            study_design=raw.get("study_design"),
            eligibility_criteria=eligibility,
            primary_endpoint=raw.get("primary_endpoint"),
            secondary_endpoints=to_json_array(raw.get("secondary_endpoints")),
            source_url=f"{self.cfg.base_url.rstrip('/')}/{raw['source_trial_id']}",
            raw_payload=json.dumps(raw, ensure_ascii=False, default=str),
        )
