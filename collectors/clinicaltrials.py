"""
ClinicalTrials.gov API v2 collector.

Uses the public API at https://clinicaltrials.gov/api/v2.

Modes
-----
- **Bootstrap** (via ``run(is_bootstrap=True)``):  365-day lookback,
  records marked ``is_bootstrap=1``.
- **Incremental** (via ``run(since=last_successful_sync)``):  72-hour
  configurable lookback window, normal records.
- **Full fetch** (``since=None``):  uses the bootstrap lookback duration
  but records are NOT marked as bootstrap.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from collectors.base import BaseCollector, NormalisedRecord
from config import CONFIG

logger = logging.getLogger(__name__)


def _fmt(d: str | None) -> str | None:
    """Normalise a date string from the API."""
    if not d:
        return None
    parts = d.strip().split("-")
    if len(parts) == 1:
        return f"{parts[0]}-01-01"
    elif len(parts) == 2:
        return f"{parts[0]}-{parts[1]}-01"
    return d.strip()


def _json_str(val: Any) -> str | None:
    if val is None:
        return None
    return json.dumps(val, ensure_ascii=False, default=str)


# ── Status / study-type / phase normalisation ─────────────────────────


_STATUS_MAP: dict[str, str] = {
    "RECRUITING": "Recruiting",
    "ACTIVE, NOT RECRUITING": "Active, not recruiting",
    "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
    "COMPLETED": "Completed",
    "NOT YET RECRUITING": "Not yet recruiting",
    "NOT_YET_RECRUITING": "Not yet recruiting",
    "ENROLLING BY INVITATION": "Enrolling by invitation",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation",
    "SUSPENDED": "Suspended",
    "TERMINATED": "Terminated",
    "WITHDRAWN": "Withdrawn",
    "UNKNOWN STATUS": "Unknown status",
    "UNKNOWN": "Unknown status",
    "AVAILABLE": "Available",
    "NO LONGER AVAILABLE": "No longer available",
    "NO_LONGER_AVAILABLE": "No longer available",
    "TEMPORARILY NOT AVAILABLE": "Temporarily not available",
    "TEMPORARILY_NOT_AVAILABLE": "Temporarily not available",
    "APPROVED FOR MARKETING": "Approved for marketing",
    "APPROVED_FOR_MARKETING": "Approved for marketing",
    "WITHHELD": "Unknown status",
}


def _normalise_nct_status(raw: str | None) -> str | None:
    if not raw:
        return None
    return _STATUS_MAP.get(raw.strip(), raw.strip().title())


_STUDY_TYPE_MAP: dict[str, str] = {
    "INTERVENTIONAL": "Interventional",
    "OBSERVATIONAL": "Observational",
    "BASIC_SCIENCE": "Basic Science",
    "DIAGNOSTIC_TEST": "Diagnostic Test",
    "HEALTH_SERVICES_RESEARCH": "Health Services Research",
    "PREVENTION": "Prevention",
    "SCREENING": "Screening",
    "OTHER": "Other",
}


def _normalise_nct_study_type(raw: str | None) -> str | None:
    if not raw:
        return None
    return _STUDY_TYPE_MAP.get(raw.strip(), raw.strip().title())


_PHASE_RE = re.compile(r"PHASE(\d)", re.IGNORECASE)


def _normalise_nct_phase(phases: list[str] | None) -> str | None:
    """Convert NCT API phase tokens to human-readable form.

    ``["PHASE1"]``           → ``"Phase 1"``
    ``["PHASE1", "PHASE2"]`` → ``"Phase 1/Phase 2"``
    ``["EARLY_PHASE1"]``     → ``"Early Phase 1"``
    ``["NA"]``               → ``"N/A"``
    """
    if not phases:
        return None
    parts: list[str] = []
    for p in phases:
        p_upper = p.strip().upper()
        if p_upper == "NA":
            parts.append("N/A")
            continue
        normalised = _PHASE_RE.sub(r"Phase \1", p_upper)
        normalised = normalised.replace("_", " ").strip()
        normalised = normalised.title()
        parts.append(normalised)
    return "/".join(parts)


class ClinicalTrialsGovCollector(BaseCollector):
    """Collector for ClinicalTrials.gov via API v2 /studies endpoint."""

    def __init__(self):
        super().__init__("clinicaltrials_gov")
        self._default_lookback_days = CONFIG.crawl.bootstrap_lookback_days

    # ── BaseCollector abstract methods ─────────────────────────────

    def fetch_new_or_updated(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch studies using the /studies endpoint with cursor pagination.

        Parameters
        ----------
        since : str or None
            ISO-8601 timestamp for incremental fetch.  When None, defaults to
            ``bootstrap_lookback_days`` (365 days) ago.
        """
        cfg = CONFIG.sources["clinicaltrials_gov"]
        api = cfg.api_base.rstrip("/")
        url = f"{api}/studies"

        # 疾病/概念检索式（全量检索核心）：API v2 query.<area>。默认 cond
        # （疾病主题按适应症检索）；生物标志物/抗体等「非疾病型」主题的
        # 概念词在标题/摘要里，用 extra["nct_search_area"]="term" 全字段检索。
        cond_query = (cfg.extra or {}).get("query_cond")
        search_area = (cfg.extra or {}).get("nct_search_area", "cond")

        # Determine the update-since filter (API v2 accepts YYYY-MM-DD).
        # 仅在做「增量」(since 给定) 时加日期窗口；bootstrap (since=None) 走
        # condition 全历史检索，实现真正的全量。
        if since is None:
            since_dt = datetime.now(timezone.utc) - timedelta(days=self._default_lookback_days)
            since_str = since_dt.strftime("%Y-%m-%d")
            logger.info("No 'since' provided — full disease-profile retrieval (all-time, condition query).")
        else:
            # Extract date portion if a full ISO timestamp was passed
            since_str = since[:10] if since and len(since) >= 10 else since

        params: Dict[str, Any] = {
            "pageSize": CONFIG.crawl.page_size,   # 100
            "format": "json",
        }

        # 全量检索：按检索式过滤（所有历史 / 增量均生效）
        if cond_query:
            params[f"query.{search_area}"] = cond_query
            logger.info("Adding disease concept query (area=%s): %s",
                        search_area, cond_query)

        # 增量检索：额外限制「最近更新日期」窗口；日期字段可按 profile 配置
        # （extra["date_field"]，默认 LastUpdatePostDate，可设为 StartDate 等）
        if since:
            date_field = (cfg.extra or {}).get("date_field", "LastUpdatePostDate")
            # Essie syntax: AREA[field]RANGE[min,MAX]
            params["filter.advanced"] = f"AREA[{date_field}]RANGE[{since_str},MAX]"

        results: List[Dict[str, Any]] = []
        next_token: Optional[str] = None
        total_fetched = 0
        # max_records_per_run == 0 视为「不限条数」（全量）
        max_records = cfg.max_records_per_run

        while True:
            if next_token:
                params["pageToken"] = next_token
            else:
                params.pop("pageToken", None)

            logger.info("Fetching page (token=%s, total_so_far=%d) …",
                        next_token, total_fetched)
            resp = requests.get(url, params=params, timeout=cfg.timeout_sec)
            resp.raise_for_status()
            body = resp.json()

            studies = body.get("studies", [])
            results.extend(studies)
            total_fetched += len(studies)

            next_token = body.get("nextPageToken")
            if not next_token or (max_records and total_fetched >= max_records):
                break

            # Polite delay between API page requests
            time.sleep(cfg.request_delay_sec)

        if since:
            logger.info("Fetched %d studies from ClinicalTrials.gov "
                        "(incremental, since=%s, cond=%s)",
                        len(results), since_str, bool(cond_query))
        else:
            # 全量检索（condition 检索式，未套日期窗口）——不要打印 since，
            # 否则会被误读成增量窗口（myocarditis 案例验证时的排障教训）
            logger.info("Fetched %d studies from ClinicalTrials.gov "
                        "(full retrieval, cond=%s, no date window)",
                        len(results), bool(cond_query))
        return results

    # ── 实时检索（live-search，server /api/trials/live-check 同步调用） ──

    def live_search(self, keyword: str, max_pages: int = 1,
                    start_token: Optional[str] = None) -> dict:
        """同步查 NCT：官方 API v2 全字段检索，命中即解析入库。

        与 WAF 源的 live_search 同一返回契约，但 NCT 是官方公开 API（无
        反爬约束）且单请求即返回完整 study JSON —— 直接 normalise +
        upsert，无补爬队列环节（queued 恒为 0，enriched = 实际入库数）。
        每页 pageSize=10，最多翻 max_pages 页（页间礼貌间隔）。注意 NCT
        内容为英文，中文关键词通常 0 命中——按原词直发，不做翻译。

        分批契约与 browser_base.live_search 一致：``start_token``（上批
        返回的 ``next_token``）续抓；返回 ``site_total``（API totalCount）、
        ``has_more`` / ``next_token`` 供调用方续批。
        """
        cfg = CONFIG.sources["clinicaltrials_gov"]
        api = cfg.api_base.rstrip("/")
        page_size = 10
        entries: List[Dict[str, Any]] = []
        pages_walked = 0
        stopped_reason = "completed"
        enriched = 0
        site_total: Optional[int] = None
        next_token: Optional[str] = start_token or None
        last_page_full = False
        max_pages = max(1, int(max_pages))
        for page in range(1, max_pages + 1):
            params: Dict[str, Any] = {"query.term": keyword,
                                      "pageSize": page_size, "format": "json",
                                      "countTotal": "true"}
            if next_token:
                params["pageToken"] = next_token
            try:
                resp = requests.get(f"{api}/studies", params=params,
                                    timeout=cfg.timeout_sec)
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                logger.warning("NCT live_search 请求失败 '%s' p%d: %s",
                               keyword, page, exc)
                stopped_reason = "request_failed"
                last_page_full = False
                break
            # totalCount 只在首页返回（pageToken 续页不带）——仅在字段
            # 出现时更新，避免被后续页覆盖回 None
            if body.get("totalCount") is not None:
                try:
                    site_total = int(body["totalCount"])
                except (TypeError, ValueError):
                    pass
            studies = body.get("studies", [])
            pages_walked += 1
            for study in studies:
                proto = study.get("protocolSection") or {}
                ident = proto.get("identificationModule") or {}
                nct_id = ident.get("nctId") or ""
                if not nct_id:
                    continue
                entries.append({
                    "source_trial_id": nct_id,
                    "title": ident.get("briefTitle") or "",
                    "url": f"https://clinicaltrials.gov/study/{nct_id}",
                })
                try:
                    self._upsert_and_emit_events(self.normalise(study))
                    enriched += 1
                except Exception as exc:
                    logger.warning("NCT live upsert %s 失败: %s", nct_id, exc)
            next_token = body.get("nextPageToken")
            last_page_full = len(studies) >= page_size and bool(next_token)
            if not last_page_full:
                break
            if page < max_pages:
                time.sleep(cfg.request_delay_sec)
        seen: set = set()
        norm_entries: List[Dict[str, Any]] = []
        for e in entries:
            if e["source_trial_id"] in seen:
                continue
            seen.add(e["source_trial_id"])
            norm_entries.append(e)
        has_more = stopped_reason == "completed" and last_page_full \
            and pages_walked > 0
        stats = {
            "keyword": keyword, "found": len(norm_entries), "queued": 0,
            "enriched": enriched, "pages_walked": pages_walked,
            "stopped_reason": stopped_reason, "site_total": site_total,
            "has_more": has_more, "next_token": next_token if has_more else None,
            "entries": norm_entries,
        }
        logger.info("NCT live_search '%s': found=%d upserted=%d pages=%d total=%s more=%s (%s)",
                    keyword, len(norm_entries), enriched, pages_walked,
                    site_total, has_more, stopped_reason)
        return stats

    def normalise(self, raw: Dict[str, Any]) -> NormalisedRecord:
        """Convert the API v2 study JSON into our NormalisedRecord.

        API v2 structure::

            {
              "protocolSection": {
                "identificationModule": { "nctId": "...", ... },
                "statusModule":       { "overallStatus": "...", ... },
                ...
              }
            }
        """
        ps = raw.get("protocolSection", {})
        id_mod = ps.get("identificationModule", {})
        des_mod = ps.get("descriptionModule", {})
        stat_mod = ps.get("statusModule", {})
        spon_mod = ps.get("sponsorCollaboratorsModule", {})
        desi_mod = ps.get("designModule", {})
        arms_mod = ps.get("armsInterventionsModule", {})
        elig_mod = ps.get("eligibilityModule", {})
        cond_mod = ps.get("conditionsModule", {})
        loc_mod = ps.get("contactsLocationsModule", {})
        outc_mod = ps.get("outcomesModule", {})

        nct_id = id_mod.get("nctId", "")

        # Status (normalise case to match DB status_types)
        status = _normalise_nct_status(stat_mod.get("overallStatus"))
        expanded_status = _normalise_nct_status(
            stat_mod.get("lastKnownStatus") or status
        )

        # Sponsor
        lead_sponsor = spon_mod.get("leadSponsor", {})
        collaborators = spon_mod.get("collaborators", [])
        sponsor_list = [{"name": lead_sponsor.get("name", ""), "role": "lead"}]
        for c in collaborators:
            sponsor_list.append({"name": c.get("name", ""), "role": "collaborator"})

        # Locations / countries
        locations = loc_mod.get("locations", [])
        country_set: set[str] = set()
        loc_list: list[dict] = []
        for loc in locations:
            country = loc.get("country", "")
            if country:
                country_set.add(country)
            loc_list.append({
                "facility": loc.get("facility", ""),
                "city": loc.get("city", ""),
                "state": loc.get("state", ""),
                "country": country,
                "status": loc.get("status", ""),
            })

        # Arms & Interventions
        arm_groups = arms_mod.get("armGroups", [])
        interventions = arms_mod.get("interventions", [])
        arm_list = []
        for a in arm_groups:
            arm_list.append({
                "label": a.get("label", ""),
                "type": a.get("type", ""),
                "description": a.get("description", ""),
            })
        intv_list = [i.get("name", "") for i in interventions]

        # Design
        design_info = desi_mod.get("designInfo", {})
        phases = desi_mod.get("phases", [])

        # Conditions
        conditions = cond_mod.get("conditions", [])

        # Dates
        start_date = _fmt(stat_mod.get("startDateStruct", {}).get("date"))
        pc_date = _fmt(stat_mod.get("primaryCompletionDateStruct", {}).get("date"))
        comp_date = _fmt(stat_mod.get("completionDateStruct", {}).get("date"))
        study_first_submit = _fmt(stat_mod.get("studyFirstSubmitDate", ""))
        last_update = stat_mod.get("lastUpdateSubmitDate", "")

        # Primary outcome (from outcomesModule in API v2).  A study may
        # declare several primary outcome measures — keep them all as a
        # JSON array; a single one stays plain text so existing stored
        # rows keep the same format (and change detection stays quiet).
        primary_outcomes = outc_mod.get("primaryOutcomes", []) or []
        primary_measures = [p.get("measure") for p in primary_outcomes if p.get("measure")]
        secondary_outcomes = outc_mod.get("secondaryOutcomes", [])

        return NormalisedRecord(
            source_trial_id=nct_id,
            title=id_mod.get("briefTitle", ""),
            scientific_title=des_mod.get("detailedDescription", ""),
            study_type=_normalise_nct_study_type(desi_mod.get("studyType")),
            status=expanded_status,
            enrollment=desi_mod.get("enrollmentInfo", {}).get("count"),
            registration_date=study_first_submit,
            start_date=start_date,
            primary_completion_date=pc_date,
            completion_date=comp_date,
            last_updated_at_source=last_update,
            conditions=_json_str(conditions),
            interventions=_json_str(intv_list),
            countries=_json_str(sorted(country_set)),
            locations=_json_str(loc_list),
            sponsors=_json_str(sponsor_list),
            study_phase=_normalise_nct_phase(phases),
            study_design=design_info.get("allocation") if design_info else None,
            eligibility_criteria=elig_mod.get("eligibilityCriteria"),
            primary_endpoint=(
                _json_str(primary_measures) if len(primary_measures) > 1
                else (primary_measures[0] if primary_measures else None)
            ),
            secondary_endpoints=_json_str([s.get("measure") for s in secondary_outcomes if s.get("measure")]),
            arm_group_interventions=_json_str(arm_list),
            source_url=f"https://clinicaltrials.gov/study/{nct_id}",
            raw_payload=json.dumps(raw, ensure_ascii=False),
        )
