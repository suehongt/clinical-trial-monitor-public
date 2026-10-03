"""
Tests for the ChiCTR (chictr.org.cn) collector.

Test cases:
  1. Parse detail HTML — standalone ChiCTR study
  2. Parse detail HTML — ChiCTR with NCT cross-registration
  3. Status mapping — Chinese → English conversion
  4. Study type mapping — Chinese → English conversion
  5. Enrollment parsing — from interventions text
  6. Outcome parsing — primary/secondary endpoint extraction
  7. Date parsing — Chinese date format conversion
  8. Helper functions — identifier extraction
  9. Normalise — NormalisedRecord produced correctly
 10. Cross-platform linking — ChiCTR↔NCT via secondary_registration_no
 11. Title similarity — not auto-merged without identifier match
 12. Identifier extraction — ChiCTR numbers found in raw_payload
"""
from __future__ import annotations

import json
import os

import pytest

from db.connection import get_connection

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_STANDALONE_HTML_PATH = os.path.join(_FIXTURE_DIR, "chictr_detail_standalone.html")
_WITH_NCT_HTML_PATH = os.path.join(_FIXTURE_DIR, "chictr_detail_with_nct.html")


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def standalone_html() -> str:
    """Load standalone ChiCTR detail page HTML fixture."""
    with open(_STANDALONE_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def with_nct_html() -> str:
    """Load ChiCTR-with-NCT detail page HTML fixture."""
    with open(_WITH_NCT_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def sources(conn):
    """Seed registry sources (NCT, ChiCTR, CTR, ICTRP)."""
    for short, full in [
        ("NCT", "ClinicalTrials.gov"),
        ("ChiCTR", "中国临床试验注册中心"),
        ("CTR", "中国药物临床试验登记与信息公示平台"),
        ("ICTRP", "WHO ICTRP"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO registry_sources (short_name, full_name) VALUES (?, ?)",
            (short, full),
        )
    conn.commit()


@pytest.fixture
def lookup_types(conn):
    """Seed minimal study types and status types."""
    for label in ["Interventional", "Observational", "Diagnostic"]:
        conn.execute("INSERT OR IGNORE INTO study_types (label) VALUES (?)", (label,))
    for label in ["Recruiting", "Completed", "Prospective registration",
                  "Retrospective registration"]:
        conn.execute("INSERT OR IGNORE INTO status_types (label) VALUES (?)", (label,))
    conn.commit()


@pytest.fixture
def conn(test_db):
    """Get connection to test database."""
    return get_connection()


# ── Helper ─────────────────────────────────────────────────────────────────


def _source_id(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


def _insert_record(conn, source_short: str, source_trial_id: str,
                   title: str = "Test Trial",
                   raw_payload: str | None = None,
                   **kw) -> int:
    """Insert a registry_record and return its record_id."""
    sid = _source_id(conn, source_short)
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, raw_payload, is_latest)
           VALUES (?, ?, ?, (SELECT status_type_id FROM status_types WHERE label = 'Recruiting'), ?, 1)""",
        (sid, source_trial_id, title,
         raw_payload or json.dumps({"id": source_trial_id})),
    )
    conn.commit()
    return cur.lastrowid


# ── Tests ──────────────────────────────────────────────────────────────────


class TestParseDetailHtml:
    """Verify the HTML parser extracts all major fields correctly."""

    def test_source_trial_id(self, standalone_html):
        """注册号 → source_trial_id."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields["source_trial_id"] == "ChiCTR2600128415"

    def test_title(self, standalone_html):
        """注册题目 → title."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("title")
        assert "克隆性造血" in fields["title"] or "肺癌" in fields["title"]

    def test_scientific_title(self, standalone_html):
        """研究课题的正式科学名称 → scientific_title."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("scientific_title")
        assert "克隆性造血" in fields["scientific_title"]

    def test_study_type_mapped(self, standalone_html):
        """研究类型 → mapped to English."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("study_type") == "Observational"

    def test_status_mapped(self, standalone_html):
        """注册号状态 → mapped to English."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("registration_status") == "Retrospective registration"

    def test_conditions(self, standalone_html):
        """研究疾病 → conditions."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("conditions")
        assert "非小细胞肺癌" in fields["conditions"] or "NSCLC" in fields["conditions"]

    def test_sponsor(self, standalone_html):
        """试验主办单位 → sponsor."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("sponsor")
        assert "上海市肺科医院" in fields["sponsor"]

    def test_study_leader(self, standalone_html):
        """研究负责人 → study_leader."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("study_leader")
        assert "陈健" in fields["study_leader"]

    def test_enrollment_parsed(self, standalone_html):
        """Enrollment extracted from interventions sample sizes."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert isinstance(fields.get("enrollment"), int)
        assert fields["enrollment"] == 216  # 108 + 108

    def test_registration_date(self, standalone_html):
        """注册时间 → parsed to ISO date."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("registration_date")
        assert fields["registration_date"].startswith("20")

    def test_primary_endpoint(self, standalone_html):
        """Primary endpoint extracted from outcomes."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("primary_endpoint")
        assert "MPR" in fields["primary_endpoint"] or "病理" in fields["primary_endpoint"]

    def test_secondary_endpoints(self, standalone_html):
        """Secondary endpoints extracted from outcomes."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("secondary_endpoints")
        assert "PFS" in fields["secondary_endpoints"] or "OS" in fields["secondary_endpoints"]

    def test_eligibility_criteria_combined(self, standalone_html):
        """纳入标准 + 排除标准 → eligibility_criteria."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("eligibility_criteria")
        assert "Inclusion" in fields["eligibility_criteria"]
        assert "Exclusion" in fields["eligibility_criteria"]

    def test_locations(self, standalone_html):
        """研究实施地点 → locations."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("locations")
        assert "上海市肺科医院" in fields["locations"]

    def test_ethics_approved(self, standalone_html):
        """是否获伦理委员会批准 → ethics_approved."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("ethics_approved") == "是"

    def test_recruitment_status_mapped(self, standalone_html):
        """征募研究对象情况 → mapped to English."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("recruitment_status") == "Recruiting"

    def test_gender_mapped(self, standalone_html):
        """性别 → mapped to English."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("gender") == "Both"

    def test_raw_html_length_recorded(self, standalone_html):
        """Raw HTML length is recorded for traceability."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("_raw_html_length", 0) > 30000

    def test_study_phase_mapped(self, standalone_html):
        """研究所处阶段 → mapped to English."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("study_phase") == "Pilot/Exploratory"

    def test_funding_source(self, standalone_html):
        """经费或物资来源 → funding_source."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("funding_source")
        assert "IIT" in fields["funding_source"] or "肺科医院" in fields["funding_source"]

    def test_study_design(self, standalone_html):
        """研究设计 → study_design."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        assert fields.get("study_design") == "队列研究"


class TestParseWithNctCrossRegistration:
    """Verify parsing of ChiCTR page with NCT cross-registration."""

    def test_secondary_registration_no(self, with_nct_html):
        """在二级注册机构或其它机构的注册号 → secondary_registration_no."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(with_nct_html, "ChiCTR2600121577")
        assert fields.get("secondary_registration_no")
        assert "NCT" in fields["secondary_registration_no"]

    def test_source_trial_id(self, with_nct_html):
        """注册号 → source_trial_id."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(with_nct_html, "ChiCTR2600121577")
        assert fields["source_trial_id"] == "ChiCTR2600121577"

    def test_title_with_nct(self, with_nct_html):
        """Title extracted correctly from NCT-linked ChiCTR."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(with_nct_html, "ChiCTR2600121577")
        assert fields.get("title")
        assert "NSM" in fields["title"] or "乳房" in fields["title"]

    def test_enrollment_with_nct(self, with_nct_html):
        """Enrollment for NCT-linked trial."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(with_nct_html, "ChiCTR2600121577")
        assert isinstance(fields.get("enrollment"), int)
        assert fields["enrollment"] == 170  # 85 + 85

    def test_interventional_trial(self, with_nct_html):
        """Interventional study type maps correctly."""
        from collectors.chictr import ChiCTRCollector
        fields = ChiCTRCollector.parse_detail_page(with_nct_html, "ChiCTR2600121577")
        assert fields.get("study_type") == "Interventional"


class TestHelperFunctions:
    """Test helper functions used by the collector."""

    def test_parse_date_iso(self):
        """Already ISO format."""
        from collectors.chictr import _parse_date
        assert _parse_date("2026-07-20") == "2026-07-20"

    def test_parse_date_chinese(self):
        """Chinese date format."""
        from collectors.chictr import _parse_date
        assert _parse_date("2026年07月20日") == "2026-07-20"

    def test_parse_date_year_month(self):
        """Year-month only."""
        from collectors.chictr import _parse_date
        assert _parse_date("2026年07月") == "2026-07-01"

    def test_parse_date_slash(self):
        """Slash format."""
        from collectors.chictr import _parse_date
        assert _parse_date("2026/07/20") == "2026-07-20"

    def test_parse_date_none(self):
        """None returns None."""
        from collectors.chictr import _parse_date
        assert _parse_date(None) is None

    def test_parse_enrollment_normal(self):
        """Sum of sample sizes."""
        from collectors.chictr import _parse_enrollment_from_interventions
        assert _parse_enrollment_from_interventions("样本量： 85 样本量： 85") == 170

    def test_parse_enrollment_none(self):
        """No enrollment data."""
        from collectors.chictr import _parse_enrollment_from_interventions
        assert _parse_enrollment_from_interventions("") is None
        assert _parse_enrollment_from_interventions(None) is None

    def test_extract_chictr_numbers(self):
        """ChiCTR number extraction."""
        from collectors.chictr import _extract_chictr_numbers
        nums = _extract_chictr_numbers("ChiCTR2600128415 and ChiCTR2400089942")
        assert "ChiCTR2600128415" in nums
        assert "ChiCTR2400089942" in nums


class TestNormalise:
    """Verify normalise() produces a valid NormalisedRecord."""

    def test_normalise_produces_record(self, standalone_html):
        """Normalise returns a NormalisedRecord with correct fields."""
        from collectors.chictr import ChiCTRCollector
        from collectors.base import NormalisedRecord

        collector = ChiCTRCollector()
        parsed = collector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        raw = {
            "chictr_no": "ChiCTR2600128415",
            "proj_id": "291119",
            "html": standalone_html,
            "parsed_fields": parsed,
        }
        nr = collector.normalise(raw)

        assert isinstance(nr, NormalisedRecord)
        assert nr.source_trial_id == "ChiCTR2600128415"
        assert nr.title
        assert "克隆性造血" in nr.title
        assert nr.study_type == "Observational"
        assert nr.enrollment == 216
        assert nr.sponsors
        assert nr.conditions
        assert nr.raw_payload == standalone_html  # HTML stored as payload

    def test_normalise_no_crash_missing_fields(self):
        """Normalise handles empty/missing fields gracefully."""
        from collectors.chictr import ChiCTRCollector
        collector = ChiCTRCollector()
        raw = {
            "chictr_no": "ChiCTR99999999",
            "proj_id": "999999",
            "html": "<html><body>Empty</body></html>",
            "parsed_fields": {"source_trial_id": "ChiCTR99999999"},
        }
        nr = collector.normalise(raw)
        assert nr.source_trial_id == "ChiCTR99999999"
        assert nr.title == ""  # defaults to empty string
        assert nr.status is None


class TestOutcomeParsing:
    """Test primary/secondary endpoint extraction."""

    def test_parse_outcomes_both(self):
        """Primary and secondary outcomes."""
        from collectors.chictr import _parse_outcomes
        text = ("指标中文名： 总生存期(OS) 指标类型： 主要指标 "
                "指标中文名： 无进展生存期(PFS) 指标类型： 次要指标")
        primary, secondary = _parse_outcomes(text)
        assert primary and "OS" in primary
        assert secondary and "PFS" in secondary

    def test_parse_outcomes_primary_only(self):
        """Only primary outcome."""
        from collectors.chictr import _parse_outcomes
        text = "指标中文名： 主要终点 指标类型： 主要指标"
        primary, secondary = _parse_outcomes(text)
        assert primary
        assert secondary is None

    def test_parse_outcomes_empty(self):
        """Empty text."""
        from collectors.chictr import _parse_outcomes
        assert _parse_outcomes("") == (None, None)
        assert _parse_outcomes(None) == (None, None)


class TestStatusPhaseMapping:
    """Test status and phase mapping logic."""

    def test_registration_status_direct(self):
        """REGISTRATION_STATUS_MAP exact match."""
        from collectors.chictr import REGISTRATION_STATUS_MAP
        assert REGISTRATION_STATUS_MAP["预注册"] == "Prospective registration"
        assert REGISTRATION_STATUS_MAP["补注册"] == "Retrospective registration"

    def test_study_type_map(self):
        """STUDY_TYPE_MAP covers key types."""
        from collectors.chictr import STUDY_TYPE_MAP
        assert STUDY_TYPE_MAP["干预性研究"] == "Interventional"
        assert STUDY_TYPE_MAP["观察性研究"] == "Observational"
        assert STUDY_TYPE_MAP["诊断试验"] == "Diagnostic"

    def test_phase_map(self):
        """STUDY_PHASE_MAP covers all phases."""
        from collectors.chictr import STUDY_PHASE_MAP
        assert STUDY_PHASE_MAP["I期"] == "Phase 1"
        assert STUDY_PHASE_MAP["III期"] == "Phase 3"
        assert STUDY_PHASE_MAP["I期/II期"] == "Phase 1/Phase 2"
        assert STUDY_PHASE_MAP["不适用"] == "Not Applicable"

    def test_recruitment_status_map(self):
        """RECRUITMENT_STATUS_MAP covers key statuses."""
        from collectors.chictr import RECRUITMENT_STATUS_MAP
        assert RECRUITMENT_STATUS_MAP["正在进行"] == "Recruiting"
        assert RECRUITMENT_STATUS_MAP["完成"] == "Completed"
        assert RECRUITMENT_STATUS_MAP["尚未开始"] == "Not yet recruiting"


class TestCrossPlatformLinking:
    """Test ChiCTR ↔ NCT cross-platform linking via entity resolution."""

    def test_chictr_cross_ref_nct_linked(self, conn, sources, lookup_types):
        """ChiCTR record with NCT in secondary_registration_no → linked to same master."""
        from core.entity_resolution import resolve_record

        # Step 1: Insert NCT record
        rid_nct = _insert_record(conn, "NCT", "NCT07372339",
                                 title="Breast Sensation after NSM Study")

        # Step 2: Insert ChiCTR record with NCT in secondary_registration_no
        chictr_payload = json.dumps({
            "chictr_no": "ChiCTR2600121577",
            "secondary_registration_no": "NCT07372339",
        })
        rid_chictr = _insert_record(conn, "ChiCTR", "ChiCTR2600121577",
                                    title="开放与腔镜下NSM联合胸肌前假体重建术后乳房感觉差异比较",
                                    raw_payload=chictr_payload)

        # Step 3: Resolve NCT → creates new master
        r_nct = resolve_record(rid_nct)
        assert r_nct["action"] == "created"

        # Step 4: Resolve ChiCTR → should find NCT cross-ref → AUTO_CONFIRMED
        r_chictr = resolve_record(rid_chictr)
        assert r_chictr["action"] == "linked", f"Expected linked, got {r_chictr}"
        assert r_chictr["match_status"] == "AUTO_CONFIRMED"
        assert r_chictr["master_trial_id"] == r_nct["master_trial_id"]

    def test_chictr_no_cross_ref_creates_separate(self, conn, sources, lookup_types):
        """ChiCTR record with no cross-ref → separate master trial."""
        from core.entity_resolution import resolve_record

        rid_nct = _insert_record(conn, "NCT", "NCT99999999",
                                 title="Completely Different Trial")
        rid_chictr = _insert_record(conn, "ChiCTR", "ChiCTR20999999",
                                    title="完全不同的中国试验",
                                    raw_payload="<html>ChiCTR20999999 only</html>")

        r_nct = resolve_record(rid_nct)
        r_chictr = resolve_record(rid_chictr)
        assert r_nct["action"] == "created"
        assert r_chictr["action"] == "created"  # separate master
        assert r_nct["master_trial_id"] != r_chictr["master_trial_id"]

    def test_title_similar_not_auto_merged(self, conn, sources, lookup_types):
        """Title-similar ChiCTR and NCT records are NOT auto-merged (POSSIBLE at most)."""
        from core.entity_resolution import resolve_record

        # NCT trial with a specific title
        rid_nct = _insert_record(conn, "NCT", "NCT05000000",
                                 title="Lung Cancer Immunotherapy with PD-1 Inhibitor Study")

        # ChiCTR with similar title but NO cross-registration number
        rid_chictr = _insert_record(conn, "ChiCTR", "ChiCTR2600123456",
                                    title="PD-1抑制剂治疗非小细胞肺癌的免疫治疗研究",
                                    raw_payload="<html>ChiCTR2600123456 only</html>")

        r_nct = resolve_record(rid_nct)
        r_chictr = resolve_record(rid_chictr)
        assert r_nct["action"] == "created"
        # Title similarity alone → either "created" (separate)
        # or "linked" with POSSIBLE (needs review)
        if r_chictr["action"] == "linked":
            assert r_chictr["match_status"] != "AUTO_CONFIRMED", \
                "Title similarity alone must not auto-confirm"

    def test_identifier_extraction_from_chictr_payload(self, conn, sources, lookup_types):
        """ChiCTR and NCT numbers in raw_payload are extracted as identifiers."""
        from core.identifier_extraction import extract_and_save_identifiers

        payload = json.dumps({
            "chictr_no": "ChiCTR2600128415",
            "secondary_registration_no": "NCT04760888",
        })
        rid = _insert_record(conn, "ChiCTR", "ChiCTR2600128415",
                             raw_payload=payload)

        ids = extract_and_save_identifiers(rid)
        id_types = {i["identifier_type"] for i in ids}
        assert "ChiCTR" in id_types, f"ChiCTR type not found in {id_types}"
        chictr_ids = [i for i in ids if i["identifier_type"] == "ChiCTR"]
        assert chictr_ids[0]["identifier_value"] == "ChiCTR2600128415"
        # NCT should also be found
        nct_ids = [i for i in ids if i["identifier_type"] == "NCT"]
        if nct_ids:
            assert nct_ids[0]["identifier_value"] == "NCT04760888"


class TestIntegration:
    """Integration-level tests combining ChiCTR collector + DB pipeline."""

    def test_full_pipeline_with_real_html(self, conn, sources, lookup_types,
                                          standalone_html):
        """Run the full normalise→upsert pipeline with real ChiCTR HTML."""
        from collectors.chictr import ChiCTRCollector

        collector = ChiCTRCollector()
        parsed = collector.parse_detail_page(standalone_html, "ChiCTR2600128415")
        raw = {
            "chictr_no": "ChiCTR2600128415",
            "proj_id": "291119",
            "html": standalone_html,
            "parsed_fields": parsed,
        }
        nr = collector.normalise(raw)

        # Verify all key fields for the DB upsert pipeline
        assert nr.source_trial_id == "ChiCTR2600128415"
        assert nr.title
        assert nr.study_type == "Observational"
        assert nr.enrollment == 216
        assert nr.compute_hash()

        # Ensure sync_status exists for ChiCTR source
        conn.execute(
            "INSERT OR IGNORE INTO sync_status (source_id) VALUES (?)",
            (_source_id(conn, "ChiCTR"),),
        )
        conn.commit()

        result = collector._upsert_record(nr, is_bootstrap=True)
        assert result["action"] in ("new", "updated", "skipped")
        assert result["record_id"] is not None

        # Verify the record was stored
        row = conn.execute(
            "SELECT source_trial_id, title, enrollment FROM registry_records "
            "WHERE record_id = ?",
            (result["record_id"],),
        ).fetchone()
        assert row is not None
        assert row["source_trial_id"] == "ChiCTR2600128415"
        assert row["enrollment"] == 216


class TestStudyTypeCoverage:
    """Verify that ALL study types required by the user are preserved."""

    def test_study_types_preserved(self):
        """Diagnostic, IVD, observational, real-world, and IIT are all mapped."""
        from collectors.chictr import STUDY_TYPE_MAP
        essential_types = [
            "干预性研究",      # Interventional
            "观察性研究",      # Observational
            "诊断试验",       # Diagnostic (= IVD diagnostic)
            "其他",          # covers real-world, IIT
            "其它",          # same
            "基础研究",       # Basic research
            "流行病学研究",    # Epidemiological research
        ]
        for t in essential_types:
            assert t in STUDY_TYPE_MAP, f"Missing study type: {t}"
            assert STUDY_TYPE_MAP[t], f"Empty mapping for: {t}"


class TestLocalApiAdapter:
    """The self-hosted ChiCTR API reuses parsing without mutating the queue."""

    def test_api_search_contract(self, monkeypatch):
        from collectors.chictr import ChiCTRCollector

        collector = ChiCTRCollector()

        def fake_page(query, page):
            assert (query, page) == ("心力衰竭", 2)
            collector._last_site_total = 25
            return [{
                "chictr_no": "ChiCTR2600128415",
                "title": "心力衰竭研究",
                "proj_id": "291119",
            }]

        monkeypatch.setattr(collector, "_search_results_page", fake_page)
        payload = collector.api_search(" 心力衰竭 ", page=2)

        assert payload["source"] == "ChiCTR"
        assert payload["site_total"] == 25
        assert payload["has_more"] is True
        assert payload["items"] == [{
            "source_trial_id": "ChiCTR2600128415",
            "title": "心力衰竭研究",
            "proj_id": "291119",
            "url": "https://www.chictr.org.cn/showproj.html?proj=291119",
        }]

    def test_api_study_contract(self, monkeypatch, standalone_html):
        from collectors.chictr import ChiCTRCollector

        collector = ChiCTRCollector()
        monkeypatch.setattr("collectors.chictr.time.sleep", lambda seconds: None)
        monkeypatch.setattr(collector, "_lookup_by_regno", lambda trial_id: [{
            "chictr_no": trial_id,
            "title": "fixture",
            "proj_id": "291119",
        }])
        monkeypatch.setattr(collector, "_fetch", lambda url: standalone_html)

        payload = collector.api_study("chictr2600128415")

        assert payload is not None
        assert payload["source_trial_id"] == "ChiCTR2600128415"
        assert payload["fields"]["enrollment"] == 216
        assert payload["canonical"]["conditions"]
        assert isinstance(payload["canonical"]["conditions"], list)
        assert "raw_payload" not in payload["canonical"]

    def test_api_study_returns_none_for_unknown_id(self, monkeypatch):
        from collectors.chictr import ChiCTRCollector

        collector = ChiCTRCollector()
        monkeypatch.setattr(collector, "_lookup_by_regno", lambda trial_id: [])
        assert collector.api_study("ChiCTR2600128415") is None


class TestLiveSearchFieldRouting:
    """live_search 按词义选字段：申办者词走 secsponsor，其余走注册题目。

    背景（2026-09-29「罗氏」案例）：searchproj 的 title 检索把标题含
    「罗氏」的无关词（罗氏菌/皮罗氏序列征）当结果返回；试验主办单位
    字段（secsponsor=，站点 searchproj.js 证实；sponsor= 是组长单位，
    勿混用）才是申办者检索。全部离线：monkeypatch _fetch_page 捕获 URL。
    """

    EMPTY_PAGE = "<html><body><div class='no-result'></div></body></html>"

    @pytest.fixture
    def url_capture(self, monkeypatch):
        from urllib.parse import parse_qs, urlparse

        from collectors.chictr import ChiCTRCollector

        collector = ChiCTRCollector()
        monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
        urls: list = []

        def fake_fetch_page(url, timeout_ms=None):
            urls.append(url)
            return self.EMPTY_PAGE

        monkeypatch.setattr(collector, "_fetch_page", fake_fetch_page)
        collector._urls = urls
        collector._qs = lambda u: parse_qs(urlparse(u).query)
        return collector

    def test_default_and_non_live_state_is_title(self, url_capture):
        """无 live_search 栈时（api_search/发现走查）恒走注册题目字段。"""
        url_capture._search_results_page("肌钙蛋白", 1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["title"] == ["肌钙蛋白"]
        assert "secsponsor" not in qs

    def test_live_search_generic_word_keeps_title(self, url_capture):
        """普通词（疾病/标志物等）→ 注册题目检索（原行为不变）。"""
        stats = url_capture.live_search("肌钙蛋白", max_pages=1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["title"] == ["肌钙蛋白"]
        assert "secsponsor" not in qs
        assert stats["field_used"] == "title"

    def test_live_search_sponsor_marker_word_routes_to_secsponsor(
            self, url_capture):
        """带机构特征词的申办者 → 试验主办单位字段（secsponsor）。"""
        stats = url_capture.live_search("上海罗氏制药有限公司", max_pages=1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["secsponsor"] == ["上海罗氏制药有限公司"]
        assert "title" not in qs
        assert stats["field_used"] == "secsponsor"

    def test_live_search_latin_sponsor_marker_routes_to_secsponsor(
            self, url_capture):
        """英文公司名（Roche Diagnostics GmbH）同样按申办者检索。"""
        url_capture.live_search("Roche Diagnostics GmbH", max_pages=1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["secsponsor"] == ["Roche Diagnostics GmbH"]

    def test_live_search_library_sponsor_bare_word_routes_to_secsponsor(
            self, url_capture, conn, lookup_types):
        """简称「罗氏」无特征词 → 靠本地库 sponsors 证据识别为申办者。"""
        rid = _insert_record(conn, "ChiCTR", "ChiCTR26999999",
                             title="罗氏注册试验")
        conn.execute(
            "UPDATE registry_records SET sponsors = ? WHERE record_id = ?",
            ('["罗氏(中国)投资有限公司"]', rid))
        conn.commit()
        url_capture.live_search("罗氏", max_pages=1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["secsponsor"] == ["罗氏"]
        assert "title" not in qs

    def test_live_search_resumed_field_hint_skips_reclassification(
            self, url_capture):
        """续批 field 提示：沿用首查生效字段，不重新分类。"""
        stats = url_capture.live_search("肌钙蛋白", max_pages=1,
                                        field="secsponsor")
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["secsponsor"] == ["肌钙蛋白"]
        assert stats["field_used"] == "secsponsor"

    def test_field_restored_after_live_search(self, url_capture):
        """live_search 结束后字段复位 title，不影响后续非 live 调用。"""
        url_capture.live_search("上海罗氏制药有限公司", max_pages=1)
        url_capture._urls.clear()
        url_capture._search_results_page("肌钙蛋白", 1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["title"] == ["肌钙蛋白"]
        assert "secsponsor" not in qs
