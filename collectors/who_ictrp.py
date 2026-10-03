"""
WHO ICTRP (International Clinical Trials Registry Platform) Collector.

ICTRP aggregates from 17+ primary registries.  It provides a search portal at
https://trialsearch.who.int with XML export capability.

Key design constraint: ICTRP is an AGGREGATOR.  Records from a primary source
(e.g. NCT123456 from ClinicalTrials.gov) should NEVER be counted as independent
discoveries.  The entity resolution module links ICTRP records back to their
original source records via (reg_name + trial_id) matching.

Supported mode: XML snapshot import
  Download an XML export from https://trialsearch.who.int/ (search, then
  "Export all trials to XML"), store it locally, and point
  ``who_ictrp.extra.xml_export_path`` in config.py at the file.  Both
  bootstrap and incremental runs read the snapshot; the upsert layer dedups
  and versions, so re-importing a newer export picks up adds and changes.

The XML export follows the ICTRP Data format.  Key elements:
    <reg_name>        — Primary registry name (e.g. "ClinicalTrials.gov")
    <trial_id>        — Trial ID in the primary registry (e.g. "NCT04760888")
    <public_title>    — Public title
    <scientific_title> — Scientific title
    <primary_sponsor> — Primary sponsor
    <recruitment_status> — e.g. "Recruiting", "Completed"
    <study_type>      — e.g. "Interventional", "Observational"
    <phase>           — e.g. "Phase 3"
    <target_size>     — Enrollment target
    <date_registration> — Registration date (dd/mm/yyyy)
    <hc_freetext>     — Health condition(s)
    <i_freetext>      — Intervention(s)
    <country>         — Country of recruitment
    <secondary_id>    — Secondary identifier(s)
    <primary_outcome> — Primary outcome
    <secondary_outcome> — Secondary outcome
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from collectors.base import BaseCollector, NormalisedRecord, to_json_array
from config import PROJECT_ROOT
from db.connection import get_connection

logger = logging.getLogger(__name__)

ICTRP_PORTAL_URL = "https://trialsearch.who.int/"
ICTRP_PORTAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
ICTRP_LIVE_SESSION_TTL_SEC = 15 * 60
ICTRP_PORTAL_RETRY_ATTEMPTS = 3

# ── Constants ──────────────────────────────────────────────────────────────

# WHO ICTRP status → internal status mapping
STATUS_MAP: dict[str, str] = {
    "recruiting": "Recruiting",
    "not recruiting": "Not yet recruiting",
    "not yet recruiting": "Not yet recruiting",
    "active, not recruiting": "Active, not recruiting",
    "completed": "Completed",
    "suspended": "Suspended",
    "terminated": "Terminated",
    "withdrawn": "Withdrawn",
    "unknown": "Unknown status",
    "awaiting confirmation": "Unknown status",
    "no longer available": "No longer available",
    "approved for marketing": "Approved for marketing",
    "enrolling by invitation": "Enrolling by invitation",
}

# Study type mapping
STUDY_TYPE_MAP: dict[str, str] = {
    "interventional": "Interventional",
    "observational": "Observational",
    "observational [patient registry]": "Observational [Patient Registry]",
    "diagnostic": "Diagnostic Test",
    "prevention": "Prevention",
    "screening": "Screening",
    "basic science": "Basic Science",
    "health services research": "Health Services Research",
    "other": "Other",
}

# Registry name → short_name mapping
REGISTRY_NAME_MAP: dict[str, str] = {
    "clinicaltrials.gov": "NCT",
    "clinicaltrials": "NCT",
    "chictr": "ChiCTR",
    "chinese clinical trial register": "ChiCTR",
    "china drug trials": "CTR",
    "chinadrugtrials": "CTR",
    "anzctr": "ANZCTR",
    "australian new zealand clinical trials registry": "ANZCTR",
    "isrctn": "ISRCTN",
    "drks": "DRKS",
    "german clinical trials register": "DRKS",
    "jprn": "JPRN",
    "japan primary registries network": "JPRN",
    "cris": "CRIS",
    "cris (republic of korea)": "CRIS",
    "trialregister.nl": "NL",
    "netherlands trial register": "NL",
    "rebec": "REBEC",
    "brazilian clinical trials registry": "REBEC",
    "pacctr": "PACTR",
    "pan african clinical trials registry": "PACTR",
    "slctr": "SLCTR",
    "sri lanka clinical trials registry": "SLCTR",
    "ctri": "CTRI",
    "clinical trials registry india": "CTRI",
    "lbctr": "LBCTR",
    "lebanese clinical trials registry": "LBCTR",
    "irct": "IRCT",
    "iranian registry of clinical trials": "IRCT",
    "tctr": "TCTR",
    "thai clinical trials registry": "TCTR",
    "rpcec": "RPCEC",
    "cuban public registry of clinical trials": "RPCEC",
    "peruvian clinical trials registry": "PER",
    "repis": "REPIS",
    "kenya clinical trials registry": "PACTR",
}

# The WHO result grid does not expose recruitment countries, but its Main ID
# identifies the primary registry that contributed each record.  Keep that
# provenance separate from trial geography: ``source_jurisdiction`` means the
# registry's home jurisdiction, not where participants are recruited.
PORTAL_REGISTRY_ORIGINS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^CTRI(?:/|\b)", re.I), "CTRI", "IN"),
    (re.compile(r"^NCT\d", re.I), "NCT", "US"),
    (re.compile(r"^ChiCTR", re.I), "ChiCTR", "CN"),
    (re.compile(r"^CTR\d", re.I), "CTR", "CN"),
    (re.compile(r"^ACTRN", re.I), "ANZCTR", "AU_NZ"),
    (re.compile(r"^ISRCTN", re.I), "ISRCTN", "GB"),
    (re.compile(r"^DRKS", re.I), "DRKS", "DE"),
    (re.compile(r"^(?:JPRN|jRCT|UMIN)", re.I), "JPRN", "JP"),
    (re.compile(r"^KCT", re.I), "CRIS", "KR"),
    (re.compile(r"^(?:NTR|NL\d)", re.I), "NTR", "NL"),
    (re.compile(r"^(?:RBR|REBEC)", re.I), "ReBEC", "BR"),
    (re.compile(r"^PACTR", re.I), "PACTR", "AFRICA"),
    (re.compile(r"^SLCTR", re.I), "SLCTR", "LK"),
    (re.compile(r"^IRCT", re.I), "IRCT", "IR"),
    (re.compile(r"^TCTR", re.I), "TCTR", "TH"),
    (re.compile(r"^RPCEC", re.I), "RPCEC", "CU"),
    (re.compile(r"^LBCTR", re.I), "LBCTR", "LB"),
    (re.compile(r"^(?:PER|REPEC)", re.I), "REPEC", "PE"),
    (re.compile(r"^(?:EUCTR|EudraCT)", re.I), "EUCTR", "EU"),
)

# ── Helper functions ───────────────────────────────────────────────────────


def _map_status(raw: str | None) -> str | None:
    """Map WHO ICTRP status text → internal status label."""
    if not raw:
        return None
    key = raw.strip().lower()
    # Try direct match
    if key in STATUS_MAP:
        return STATUS_MAP[key]
    # Try partial match
    for k, v in STATUS_MAP.items():
        if k in key:
            return v
    return None


def _map_study_type(raw: str | None) -> str | None:
    """Map WHO ICTRP study_type → internal study_type label."""
    if not raw:
        return None
    key = raw.strip().lower()
    if key in STUDY_TYPE_MAP:
        return STUDY_TYPE_MAP[key]
    # Try partial
    for k, v in STUDY_TYPE_MAP.items():
        if k in key:
            return v
    return None


def _normalize_registry_name(raw: str) -> str | None:
    """Map a WHO registry name to our short_name (e.g. 'ClinicalTrials.gov' → 'NCT')."""
    if not raw:
        return None
    key = raw.strip().lower()
    # Direct match
    if key in REGISTRY_NAME_MAP:
        return REGISTRY_NAME_MAP[key]
    # Partial match for common patterns
    if "clinicaltrials" in key:
        return "NCT"
    if "chictr" in key or "chinese clinical" in key:
        return "ChiCTR"
    if "chinadrugtrials" in key or "drug trials" in key:
        return "CTR"
    if "anzctr" in key or "australian" in key:
        return "ANZCTR"
    if "isrctn" in key:
        return "ISRCTN"
    if "drks" in key or "german" in key:
        return "DRKS"
    if "jprn" in key or "japan" in key:
        return "JPRN"
    if "cris" in key and "korea" in key:
        return "CRIS"
    if "netherlands" in key or "trialregister" in key:
        return "NL"
    if "rebec" in key or "brazil" in key:
        return "REBEC"
    if "pacctr" in key or "african" in key or "kenya" in key:
        return "PACTR"
    if "slctr" in key or "sri lanka" in key:
        return "SLCTR"
    if "ctri" in key or "india" in key:
        return "CTRI"
    if "lbctr" in key or "lebanese" in key:
        return "LBCTR"
    if "irct" in key or "iran" in key:
        return "IRCT"
    if "tctr" in key or "thai" in key:
        return "TCTR"
    if "rpcec" in key or "cuban" in key:
        return "RPCEC"
    return None


def _parse_date_who(raw: str | None) -> str | None:
    """Parse WHO date (dd/mm/yyyy or other) → ISO (yyyy-mm-dd)."""
    if not raw:
        return None
    raw = raw.strip()
    # dd/mm/yyyy (day/month may be 1 or 2 digits)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # yyyy-mm-dd
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    # Other
    return raw[:10] if len(raw) >= 10 else raw


def _parse_int_who(raw: str | None) -> int | None:
    """Parse integer from WHO text field."""
    if not raw:
        return None
    m = re.search(r"\d+", raw.replace(",", ""))
    return int(m.group()) if m else None


def _extract_source_trial_id(reg_name: str | None, trial_id: str | None) -> str | None:
    """Construct a canonical source_trial_id from registry name + trial ID.

    For known registries, returns the canonical ID (e.g. NCT04760888).
    Trial IDs that already carry their registry's ID scheme (e.g. ACTRN… for
    ANZCTR) are returned unchanged instead of being double-prefixed.
    For unknown registries, returns 'reg_name:trial_id'.
    """
    if not trial_id:
        return None
    trial_id = trial_id.strip()
    prefix = _normalize_registry_name(reg_name)
    if prefix:
        if trial_id.upper().startswith(prefix.upper()):
            return trial_id
        # registry-specific alias ID schemes that must not be re-prefixed
        alias_schemes: dict[str, tuple[str, ...]] = {"ANZCTR": ("ACTRN",)}
        if trial_id.upper().startswith(alias_schemes.get(prefix, ())):
            return trial_id
        return f"{prefix}{trial_id}"
    return trial_id


def _portal_registry_origin(trial_id: str) -> dict[str, str]:
    """Infer WHO portal provenance from its primary-registry Main ID."""
    value = (trial_id or "").strip()
    for pattern, registry, jurisdiction in PORTAL_REGISTRY_ORIGINS:
        if pattern.search(value):
            return {
                "source_registry": registry,
                "source_jurisdiction": jurisdiction,
            }
    return {}


def _portal_hidden_fields(html: str) -> dict[str, str]:
    """Return the ASP.NET state required for search and pager postbacks."""
    fields = dict(re.findall(
        r'name="([A-Za-z_][A-Za-z_0-9]*)"[^>]*value="([^"]*)"', html,
    ))
    return {
        key: value for key, value in fields.items()
        if key.startswith("__") or key == "ToolkitScriptManager_HiddenField"
    }


def _portal_request(session: requests.Session, method: str,
                    **kwargs) -> requests.Response:
    """Issue one portal request with bounded transport-only retries."""
    last_error: Optional[requests.RequestException] = None
    for attempt in range(ICTRP_PORTAL_RETRY_ATTEMPTS):
        try:
            response = session.request(method, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < ICTRP_PORTAL_RETRY_ATTEMPTS:
                time.sleep(attempt + 1)
    assert last_error is not None
    raise last_error


class _ICTRPGridParser(HTMLParser):
    """Extract the ten lightweight result rows from WHO's GridView1 table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._grid_depth = 0
        self._in_row = False
        self._in_cell = False
        self._cells: list[str] = []
        self._cell_text: list[str] = []
        self._trial_href: Optional[str] = None
        self._trial_title: list[str] = []
        self._in_trial_link = False
        self.entries: list[dict[str, Any]] = []

    @staticmethod
    def _attrs(attrs) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs) -> None:
        values = self._attrs(attrs)
        if tag == "table":
            if self._grid_depth:
                self._grid_depth += 1
            elif values.get("id") == "GridView1":
                self._grid_depth = 1
            return
        if not self._grid_depth:
            return
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._trial_href = None
            self._trial_title = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_text = []
        elif tag == "a" and self._in_row:
            href = values.get("href", "")
            if "Trial2.aspx?" in href:
                self._trial_href = href
                self._trial_title = []
                self._in_trial_link = True

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)
        if self._in_trial_link:
            self._trial_title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._grid_depth:
            self._grid_depth -= 1
            return
        if not self._grid_depth:
            return
        if tag == "a" and self._in_trial_link:
            self._in_trial_link = False
        elif tag in ("td", "th") and self._in_cell:
            self._cells.append(" ".join("".join(self._cell_text).split()))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self._finish_row()
            self._in_row = False

    def _finish_row(self) -> None:
        if not self._trial_href:
            return
        query = parse_qs(urlparse(self._trial_href).query)
        trial_id = (query.get("TrialID") or query.get("trialid") or [""])[0].strip()
        if not trial_id:
            return
        title = " ".join("".join(self._trial_title).split())
        self.entries.append({
            "source_trial_id": trial_id,
            "title": title or trial_id,
            "url": urljoin(ICTRP_PORTAL_URL, self._trial_href),
            "status": self._cells[0] if self._cells else None,
            "registration_date": self._cells[5] if len(self._cells) > 5 else None,
            **_portal_registry_origin(trial_id),
        })


def _parse_portal_results(html: str) -> dict[str, Any]:
    """Parse one WHO result page and its bridged trial/record totals."""
    parser = _ICTRPGridParser()
    parser.feed(html)
    match = re.search(
        r"([\d,]+)\s+records\s+for\s+([\d,]+)\s+trials\s+found",
        html, flags=re.IGNORECASE,
    )
    records_total = int(match.group(1).replace(",", "")) if match else len(parser.entries)
    trials_total = int(match.group(2).replace(",", "")) if match else len(parser.entries)
    return {
        "records_total": records_total,
        "trials_total": trials_total,
        "entries": parser.entries,
    }


# ── Collector ──────────────────────────────────────────────────────────────


class WHOICTRPCollector(BaseCollector):
    """Collector for WHO ICTRP aggregated registry.

    Fetch mode: XML snapshot import (see module docstring).
    """

    def __init__(self):
        super().__init__("who_ictrp")
        self._source_id: int | None = None
        # Query -> requests.Session + current GridView page.  The FastAPI
        # adapter serializes access, so sessions can safely continue ASP.NET
        # postbacks for "load more" without repeating the initial search.
        self._live_sessions: dict[str, dict[str, Any]] = {}

    def _portal_start(self, query: str) -> dict[str, Any]:
        session = requests.Session()
        session.headers.update({
            "User-Agent": ICTRP_PORTAL_UA,
            "Origin": "https://trialsearch.who.int",
            "Referer": ICTRP_PORTAL_URL,
        })
        response = _portal_request(
            session, "GET", url=ICTRP_PORTAL_URL, timeout=30,
        )
        fields = _portal_hidden_fields(response.text)
        fields.update({"TextBox1": query, "Button1": "Search"})
        response = _portal_request(
            session, "POST", url=ICTRP_PORTAL_URL,
            data=fields, timeout=60,
        )
        if "Error Page" in response.text[:2000]:
            raise RuntimeError("WHO ICTRP portal returned an error page")
        return {
            "session": session,
            "html": response.text,
            "page": 1,
            "last_used": time.monotonic(),
        }

    @staticmethod
    def _portal_page(state: dict[str, Any], page: int) -> None:
        fields = _portal_hidden_fields(state["html"])
        fields.update({
            "__EVENTTARGET": "GridView1",
            "__EVENTARGUMENT": f"Page${page}",
        })
        response = _portal_request(
            state["session"], "POST", url=ICTRP_PORTAL_URL,
            data=fields, timeout=60,
        )
        if "Error Page" in response.text[:2000]:
            raise RuntimeError("WHO ICTRP portal returned an error page")
        state.update({
            "html": response.text,
            "page": page,
            "last_used": time.monotonic(),
        })

    def live_search(self, keyword: str, max_pages: int = 1,
                    start_page: int = 1) -> dict[str, Any]:
        """Search WHO's public portal through its lightweight GridView pages.

        This is a private adapter over the public search form, not WHO's
        restricted web service.  It deliberately returns list metadata only;
        the weekly XML snapshot remains the full-detail ingestion path.
        """
        query = keyword.strip()[:200]
        if not query:
            raise ValueError("keyword is blank")
        max_pages = min(max(int(max_pages), 1), 5)
        start_page = max(int(start_page), 1)
        now = time.monotonic()
        for key, cached in list(self._live_sessions.items()):
            if now - cached["last_used"] >= ICTRP_LIVE_SESSION_TTL_SEC:
                cached["session"].close()
                del self._live_sessions[key]

        cache_key = query.casefold()
        state = self._live_sessions.get(cache_key)
        if state is None or start_page == 1 or state["page"] > start_page:
            if state is not None:
                state["session"].close()
            state = self._portal_start(query)
            self._live_sessions[cache_key] = state

        while state["page"] < start_page:
            self._portal_page(state, state["page"] + 1)

        entries: list[dict[str, Any]] = []
        pages_walked = 0
        records_total = trials_total = 0
        current_page = state["page"]
        while pages_walked < max_pages:
            parsed = _parse_portal_results(state["html"])
            records_total = parsed["records_total"]
            trials_total = parsed["trials_total"]
            entries.extend(parsed["entries"])
            pages_walked += 1
            total_pages = max((trials_total + 9) // 10, 1)
            if current_page >= total_pages:
                break
            if pages_walked >= max_pages:
                break
            current_page += 1
            self._portal_page(state, current_page)

        total_pages = max((trials_total + 9) // 10, 1)
        has_more = current_page < total_pages
        state["last_used"] = time.monotonic()
        return {
            "keyword": query,
            "found": len(entries),
            "queued": 0,
            "enriched": 0,
            "pages_walked": pages_walked,
            "stopped_reason": "page_limit" if has_more else "completed",
            "site_total": trials_total,
            "records_total": records_total,
            "has_more": has_more,
            "next_page": current_page + 1 if has_more else None,
            "next_token": None,
            "entries": entries,
        }

    def _latest_record_is_full(self, trial_id: str) -> bool:
        """True when the library's latest row for ``trial_id`` came from the
        XML snapshot rather than a previous portal live search.

        The portal grid exposes strictly fewer fields than the snapshot, so
        upserting a live row over a full record would version the record
        backwards — countries/sponsors/conditions/phase/enrollment would all
        vanish until the next weekly snapshot re-versioned it.
        """
        conn = get_connection()
        # json_valid must gate json_extract: a corrupted/malformed payload
        # row would otherwise raise and take the whole live ingest down.
        # Latest rows are ordered by version so a stray duplicate-latest
        # row cannot shadow the real one.
        row = conn.execute(
            "SELECT CASE WHEN raw_payload IS NULL OR json_valid(raw_payload) = 0 "
            "THEN NULL ELSE json_extract(raw_payload, "
            "'$.who_ictrp_record._portal_live') END "
            "FROM registry_records "
            "WHERE source_id = ? AND source_trial_id = ? AND is_latest = 1 "
            "ORDER BY version_number DESC LIMIT 1",
            (self.source_id, trial_id),
        ).fetchone()
        # NULL raw_payload also counts as full: only live rows carry the flag.
        return row is not None and not row[0]

    def ingest_live_entries(self, entries: list[dict[str, Any]]) -> int:
        """Persist every lightweight WHO portal hit in the ICTRP library.

        The search grid exposes fewer fields than the periodic XML snapshot,
        but it does provide a stable primary-registry ID, title, recruitment
        status and registration date.  Saving those rows makes an explicit
        full live search complete and searchable immediately; a later XML
        import versions the same ICTRP record with the remaining fields.

        Hits whose library record is already the full snapshot row are
        skipped: a thin portal row must never supersede it (the snapshot
        stays the status/detail authority for those).
        """
        ingested = 0
        skipped_full = 0
        for entry in entries:
            trial_id = str(entry.get("source_trial_id") or "").strip()
            if not trial_id:
                continue
            if self._latest_record_is_full(trial_id):
                skipped_full += 1
                continue
            raw = {
                "reg_name": entry.get("source_registry") or "WHO ICTRP",
                "trial_id": trial_id,
                "public_title": entry.get("title") or trial_id,
                "recruitment_status": entry.get("status") or "",
                "date_registration": entry.get("registration_date") or "",
                "_portal_live": True,
                "_portal_url": entry.get("url"),
            }
            try:
                self._upsert_and_emit_events(self.normalise(raw))
                ingested += 1
            except Exception as exc:
                logger.warning("WHO live upsert %s failed: %s", trial_id, exc)
        if skipped_full:
            logger.info(
                "WHO live ingest: %d stored, %d skipped (full snapshot "
                "record already in library)", ingested, skipped_full)
        return ingested

    # ── XML parsing ─────────────────────────────────────────────────────

    @staticmethod
    def parse_xml(xml_content: str) -> List[Dict[str, Any]]:
        """Parse WHO ICTRP XML export into a list of record dicts.

        Handles both the older v1.3 format (<trial_id>, <reg_name>) and
        the newer v1.4+ export format (<TrialID>, <Source_Register>, etc.).
        """
        # Tag name normalisation map: XML tag (lowercase) → canonical field name
        TAG_ALIASES = {
            "trialid": "trial_id",
            "source_register": "reg_name",
            "condition": "hc_freetext",
            "intervention": "i_freetext",
            "countries": "country",
        }

        records = []
        root = ET.fromstring(xml_content)

        # Group: each unique trial_id + reg_name forms a record
        # Some elements repeat (secondary_id, country, etc.) — collect as lists
        LIST_TAGS = frozenset({
            "secondary_id", "country", "country2",
            "hc_code", "i_code", "secondary_outcome",
            "secondarysponsor", "source_name",
        })

        for trial_elem in root.iter():
            if trial_elem.tag not in ("Trial", "trial"):
                continue
            rec: Dict[str, Any] = {}
            for child in trial_elem:
                raw_tag = child.tag.lower()
                tag = TAG_ALIASES.get(raw_tag, raw_tag)
                text = (child.text or "").strip()
                if tag in LIST_TAGS:
                    rec.setdefault(tag, []).append(text)
                else:
                    rec[tag] = text
            if rec.get("trial_id") or rec.get("trialid"):
                # Ensure trial_id is always set to canonical key
                if "trial_id" not in rec and "trialid" in rec:
                    rec["trial_id"] = rec["trialid"]
                records.append(rec)

        # If no <Trial> elements found, try flat structure
        if not records:
            rec: Dict[str, Any] = {}
            for child in root.iter():
                if child.tag in ("Trial", "trial"):
                    continue
                raw_tag = child.tag.lower()
                tag = TAG_ALIASES.get(raw_tag, raw_tag)
                text = (child.text or "").strip()
                if tag in LIST_TAGS:
                    rec.setdefault(tag, []).append(text)
                else:
                    rec[tag] = text
            if rec.get("trial_id") or rec.get("trialid"):
                if "trial_id" not in rec and "trialid" in rec:
                    rec["trial_id"] = rec["trialid"]
                records.append(rec)

        return records

    # ── XML file import ─────────────────────────────────────────────────

    @staticmethod
    def read_xml_file(filepath: str) -> str:
        """Read a WHO ICTRP XML export file."""
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    # ── BaseCollector interface ─────────────────────────────────────────

    def fetch_new_or_updated(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load records from the configured WHO ICTRP XML snapshot.

        ``since`` is accepted for the BaseCollector interface but unused:
        incremental runs re-read the same snapshot and rely on the upsert
        layer to dedup.  Import a newer export to pick up changes.

        Returns a list of raw record dicts (possibly empty when no snapshot
        is configured, with a logged explanation).
        """
        xml_path = self.cfg.extra.get("xml_export_path", "")
        if not xml_path:
            logger.warning(
                "No WHO ICTRP XML export configured. To enable WHO ICTRP: "
                "download an XML export from https://trialsearch.who.int/ "
                "(search, then 'Export all trials to XML') and set "
                "sources.who_ictrp.extra.xml_export_path in config.py."
            )
            return []

        path = Path(xml_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            logger.warning(
                "WHO ICTRP XML export not found at %s — download one from "
                "https://trialsearch.who.int/ or fix "
                "sources.who_ictrp.extra.xml_export_path in config.py.",
                path,
            )
            return []

        logger.info("Loading WHO ICTRP snapshot: %s", path)
        xml_content = self.read_xml_file(str(path))
        raw_records = self.parse_xml(xml_content)
        logger.info("Parsed %d records from XML snapshot", len(raw_records))
        return raw_records

    # ── Normalise ───────────────────────────────────────────────────────

    def normalise(self, raw: Dict[str, Any]) -> NormalisedRecord:
        """Convert a WHO ICTRP raw dict into a NormalisedRecord.

        The raw dict comes from XML export parsing.  Key fields:
            reg_name, trial_id, public_title, scientific_title,
            primary_sponsor, recruitment_status, study_type, phase,
            target_size, date_registration, hc_freetext, i_freetext,
            country, country2, secondary_id,
            primary_outcome, secondary_outcome,
            inclusion_criteria, exclusion_criteria,
            ethics_status, results_summary
        """
        # Determine primary registry and source trial ID
        reg_name = raw.get("reg_name", "")
        trial_id = raw.get("trial_id", "")
        source_trial_id = _extract_source_trial_id(reg_name, trial_id) or trial_id

        # Build the full identifier info for AGGREGATOR tracking
        aggregator_info = {
            "_who_source_registry": reg_name,
            "_who_source_trial_id": trial_id,
            "_who_registry_short": _normalize_registry_name(reg_name),
        }

        # Parse conditions / interventions (canonical JSON array format;
        # hc_freetext / i_freetext may be a list or a single string)
        conditions = to_json_array(raw.get("hc_freetext", ""))
        interventions = to_json_array(raw.get("i_freetext", ""))

        # Parse countries
        countries_raw = raw.get("country2", raw.get("country", ""))
        if isinstance(countries_raw, list):
            countries_str = json.dumps(countries_raw, ensure_ascii=False)
        elif countries_raw:
            countries_str = json.dumps([countries_raw], ensure_ascii=False)
        else:
            countries_str = None

        # Parse sponsors
        sponsor = raw.get("primary_sponsor", "")
        sponsors_str = json.dumps([sponsor], ensure_ascii=False) if sponsor else None

        # Parse eligibility criteria
        inclusion = raw.get("inclusion_criteria", "")
        exclusion = raw.get("exclusion_criteria", "")
        eligibility = None
        if inclusion or exclusion:
            parts = []
            if inclusion:
                parts.append(f"Inclusion Criteria:\n{inclusion}")
            if exclusion:
                parts.append(f"Exclusion Criteria:\n{exclusion}")
            eligibility = "\n\n".join(parts)

        # Parse status
        status = _map_status(raw.get("recruitment_status", ""))

        # Parse study type
        study_type = _map_study_type(raw.get("study_type", ""))

        # Parse phase
        phase = raw.get("phase", "")

        # Parse enrollment
        enrollment = _parse_int_who(raw.get("target_size", ""))

        # Parse dates
        registration_date = _parse_date_who(raw.get("date_registration", ""))

        # Parse outcomes
        primary_outcome = raw.get("primary_outcome", "")
        secondary_outcome = to_json_array(raw.get("secondary_outcome", ""))

        # Parse secondary IDs
        secondary_ids_raw = raw.get("secondary_id", [])
        if isinstance(secondary_ids_raw, str):
            secondary_ids_raw = [secondary_ids_raw]

        # Build raw_payload — keep the original XML record dict + AGGREGATOR metadata
        raw_payload = {
            "who_ictrp_record": {
                k: v for k, v in raw.items()
                if k not in ("hc_freetext", "i_freetext", "secondary_id", "country", "country2")
            },
            "_aggregator": aggregator_info,
            "secondary_ids": secondary_ids_raw,
        }

        nr = NormalisedRecord(
            source_trial_id=source_trial_id or trial_id,
            title=raw.get("public_title", raw.get("scientific_title", "")),
            scientific_title=raw.get("scientific_title", None),
            last_updated_at_source=raw.get("last_refreshed_on"),
            study_type=study_type,
            status=status,
            enrollment=enrollment,
            registration_date=registration_date,
            conditions=conditions,
            interventions=interventions,
            countries=countries_str,
            sponsors=sponsors_str,
            study_phase=phase,
            eligibility_criteria=eligibility,
            primary_endpoint=primary_outcome if primary_outcome else None,
            secondary_endpoints=secondary_outcome,
            raw_payload=json.dumps(raw_payload, ensure_ascii=False),
            # Per-trial page on the ICTRP portal (human-readable HTML).
            # NOT the TextBox1 search URL: opening it lands on the portal's
            # bare search form (ASP.NET postback), not a trial page.
            source_url=(
                "https://trialsearch.who.int/Trial3.aspx?"
                + urlencode({"trialid": trial_id})
            ),
        )

        return nr

    # ── Override _upsert_record for AGGREGATOR handling ─────────────────

    def _upsert_record(self, norm: NormalisedRecord,
                       is_bootstrap: bool = False) -> Dict[str, Any]:
        """Insert/update a WHO ICTRP record with AGGREGATOR logic.

        For AGGREGATOR sources:
          - Records are tracked but NOT counted as independent discoveries
          - is_bootstrap is always set to 1 (excluded from "new" counts)
          - Entity resolution links via (reg_name, trial_id) instead of source_trial_id
        """
        # Always mark WHO ICTRP records as bootstrap (not counted as new discoveries)
        return super()._upsert_record(norm, is_bootstrap=True)

    # ── Override run for AGGREGATOR reporting ───────────────────────────

    def run(self, since: Optional[str] = None,
            is_bootstrap: bool = False) -> Dict[str, int]:
        """Execute WHO ICTRP crawl cycle.

        Always forces is_bootstrap=True since ICTRP is an AGGREGATOR.
        """
        summary = super().run(since=since, is_bootstrap=True)

        # Add AGGREGATOR-specific stats
        summary["source_type"] = "AGGREGATOR"
        summary["note"] = (
            "WHO ICTRP is an aggregator. All records are marked is_bootstrap=1 "
            "and are excluded from new-trial counts. Run entity resolution to "
            "link ICTRP records to existing master trials."
        )
        return summary
