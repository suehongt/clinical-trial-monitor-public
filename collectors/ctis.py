"""EU Clinical Trials Information System (CTIS) public API collector."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from collectors.base import BaseCollector, NormalisedRecord, or_terms, to_json_array
from collectors.http import RequestPacer

logger = logging.getLogger(__name__)


def _date(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.rstrip("Z"), fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else None


def _stamp(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        day = _date(text)
        return f"{day} 00:00:00" if day else None


def _status(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    # Search summaries use numeric public-status codes; code 2 is the
    # authorised state returned as text by the detail endpoint.
    if text == "2":
        return "Registered"
    if text.isdigit():
        return None
    key = text.casefold()
    if "recruit" in key and not any(x in key for x in ("not recruit", "no recruit", "ended")):
        return "Recruiting"
    if any(x in key for x in ("not yet recruiting", "recruitment not started")):
        return "Not yet recruiting"
    if any(x in key for x in ("completed", "end of trial", "ended")):
        return "Completed"
    if "withdraw" in key:
        return "Withdrawn"
    if any(x in key for x in ("suspend", "temporarily halt")):
        return "Suspended"
    if any(x in key for x in ("authorised", "authorized")):
        return "Registered"
    if any(x in key for x in ("evaluation", "submitted", "pre-author")):
        return "Pre-registration"
    return text or None


def _unique(values: List[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in out:
            out.append(text)
    return out


class CTISCollector(BaseCollector):
    """Collect public CTIS search hits and enrich each with its detail record."""

    allow_empty_response = True

    def __init__(self):
        super().__init__("ctis")

    def fetch_new_or_updated(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        api = self.cfg.api_base.rstrip("/")
        page = 1
        size = min(100, max(1, self.cfg.max_records_per_run or 100))
        records: List[Dict[str, Any]] = []
        pacer = RequestPacer(self.cfg.request_delay_sec)
        detail_workers = max(1, min(8, int(
            (self.cfg.extra or {}).get("detail_workers", 4))))
        query = ",".join(or_terms((self.cfg.extra or {}).get("query_cond") or ""))
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning("Ignoring invalid CTIS incremental cursor: %s", since)

        while True:
            body = {
                "pagination": {"page": page, "size": size},
                "searchCriteria": {"containAny": query},
                "sort": {"property": "decisionDate", "direction": "DESC"},
            }
            pacer.wait()
            response = requests.post(f"{api}/search", json=body, timeout=self.cfg.timeout_sec)
            response.raise_for_status()
            payload = response.json()
            hits = payload.get("data") or payload.get("trials") or payload.get("content") or []
            if isinstance(hits, dict):
                hits = hits.get("content") or hits.get("results") or []
            eligible: List[Dict[str, Any]] = []
            for summary in hits:
                trial_id = summary.get("ctNumber") or summary.get("ctisId")
                if not trial_id:
                    continue
                updated = _stamp(summary.get("lastUpdated") or summary.get("lastPublicationUpdate"))
                if since_dt and updated:
                    updated_dt = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    # CTIS search metadata is day-resolution. Include the
                    # cursor day so an afternoon watermark cannot hide a
                    # record updated earlier that same calendar day.
                    if updated_dt.date() < since_dt.date():
                        continue
                eligible.append(summary)
                if (self.cfg.max_records_per_run
                        and len(records) + len(eligible) >= self.cfg.max_records_per_run):
                    break

            def fetch_detail(summary: Dict[str, Any]) -> Dict[str, Any]:
                trial_id = summary.get("ctNumber") or summary.get("ctisId")
                try:
                    pacer.wait()
                    detail_response = requests.get(
                        f"{api}/retrieve/{trial_id}", timeout=self.cfg.timeout_sec)
                    detail_response.raise_for_status()
                    detail = detail_response.json()
                except requests.RequestException as exc:
                    logger.warning("CTIS detail failed for %s; keeping search summary: %s", trial_id, exc)
                    detail = {}
                return {"_search": summary, "_detail": detail}

            # map() preserves registry search order.  The bounded pool hides
            # response latency; RequestPacer still controls request starts.
            with ThreadPoolExecutor(max_workers=detail_workers) as executor:
                records.extend(executor.map(fetch_detail, eligible))
            if self.cfg.max_records_per_run and len(records) >= self.cfg.max_records_per_run:
                return records[:self.cfg.max_records_per_run]

            pagination = payload.get("pagination") or payload.get("page") or {}
            total_pages = pagination.get("totalPages") or payload.get("totalPages")
            next_page = pagination.get("nextPage")
            if not hits or (total_pages and page >= int(total_pages)) or next_page is None and not total_pages:
                break
            # The live API currently returns nextPage as a boolean, not the
            # next page number. Accept either representation.
            if isinstance(next_page, bool):
                page += 1
            else:
                page = int(next_page) if next_page is not None else page + 1
        return records

    def normalise(self, raw: Dict[str, Any]) -> NormalisedRecord:
        summary = raw.get("_search") or {}
        detail = raw.get("_detail") or {}
        application = detail.get("authorizedApplication") or {}
        part_i = application.get("authorizedPartI") or {}
        trial_details = part_i.get("trialDetails") or {}
        identifiers = trial_details.get("clinicalTrialIdentifiers") or {}
        info = trial_details.get("trialInformation") or {}

        conditions_block = info.get("medicalCondition") or trial_details.get("medicalCondition") or {}
        conditions = _unique(
            [x.get("medicalCondition") for x in conditions_block.get("partIMedicalConditions") or []]
            + [x.get("termName") for x in conditions_block.get("meddraConditionTerms") or []]
        )
        products = part_i.get("products") or []
        interventions = _unique(
            [p.get("productName") for p in products]
            + [(p.get("productDictionaryInfo") or {}).get("activeSubstanceName") for p in products]
        )
        if not interventions and summary.get("product"):
            interventions = _unique(str(summary["product"]).split(","))

        parts_ii = application.get("authorizedPartsII") or []
        countries: List[str] = []
        locations: List[dict] = []
        for part in parts_ii:
            country = (part.get("mscInfo") or {}).get("countryName")
            if country and country not in countries:
                countries.append(country)
            for site in part.get("trialSites") or []:
                org_info = site.get("organisationAddressInfo") or {}
                address = org_info.get("address") or {}
                locations.append({
                    "facility": (org_info.get("organisation") or {}).get("name"),
                    "city": address.get("city"),
                    "postal_code": address.get("postcode"),
                    "country": address.get("countryName") or country,
                })

        sponsors = _unique([
            ((item.get("organisation") or {}).get("name"))
            for item in part_i.get("sponsors") or []
        ])
        endpoint = info.get("endPoint") or trial_details.get("endPoint") or {}
        primary = _unique([x.get("endPoint") for x in endpoint.get("primaryEndPoints") or []])
        secondary = _unique([x.get("endPoint") for x in endpoint.get("secondaryEndPoints") or []])
        eligibility = info.get("eligibilityCriteria") or trial_details.get("eligibilityCriteria") or {}
        inclusion = _unique([x.get("principalInclusionCriteria") for x in eligibility.get("principalInclusionCriteria") or []])
        exclusion = _unique([x.get("principalExclusionCriteria") for x in eligibility.get("principalExclusionCriteria") or []])
        eligibility_text = ""
        if inclusion:
            eligibility_text += "Inclusion:\n" + "\n".join(inclusion)
        if exclusion:
            eligibility_text += ("\n\n" if eligibility_text else "") + "Exclusion:\n" + "\n".join(exclusion)

        duration = info.get("trialDuration") or trial_details.get("trialDuration") or {}
        category = info.get("trialCategory") or {}
        phase = summary.get("trialPhase")
        if not phase and category.get("trialPhase"):
            phase = f"CTIS phase code {category['trialPhase']}"
        enrollment = summary.get("totalNumberEnrolled")
        try:
            enrollment_value = int(str(enrollment).replace(",", "")) if enrollment not in (None, "") else None
        except ValueError:
            enrollment_value = None

        trial_id = detail.get("ctNumber") or summary.get("ctNumber") or summary.get("ctisId")
        if not trial_id:
            raise ValueError("CTIS record is missing ctNumber")
        title = identifiers.get("publicTitle") or summary.get("ctTitle") or identifiers.get("fullTitle") or trial_id
        return NormalisedRecord(
            source_trial_id=str(trial_id),
            title=str(title),
            scientific_title=identifiers.get("fullTitle"),
            study_type="Interventional",
            status=_status(detail.get("ctStatus") or summary.get("ctStatus")),
            enrollment=enrollment_value,
            registration_date=_date(detail.get("publishDate") or detail.get("decisionDate") or summary.get("decisionDateOverall")),
            start_date=_date(duration.get("estimatedRecruitmentStartDate")),
            primary_completion_date=_date(duration.get("estimatedGlobalEndOfTrialDate")),
            completion_date=_date(duration.get("estimatedEndDate") or duration.get("globalEndOfTrialDate")),
            last_updated_at_source=_stamp(summary.get("lastUpdated") or summary.get("lastPublicationUpdate") or detail.get("publishDate")),
            conditions=to_json_array(conditions),
            interventions=to_json_array(interventions),
            countries=to_json_array(countries),
            locations=json.dumps(locations, ensure_ascii=False) if locations else None,
            sponsors=to_json_array(sponsors or ([summary.get("sponsor")] if summary.get("sponsor") else [])),
            study_phase=phase,
            study_design=summary.get("trialCategory"),
            eligibility_criteria=eligibility_text or None,
            primary_endpoint="\n".join(primary) if primary else summary.get("primaryEndPoint"),
            secondary_endpoints=to_json_array(secondary),
            source_url=f"{self.cfg.base_url.rstrip('/')}/view/{trial_id}",
            raw_payload=json.dumps(raw, ensure_ascii=False, default=str),
        )
