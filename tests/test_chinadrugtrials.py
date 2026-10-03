"""
Tests for the CDE (chinadrugtrials.org.cn) collector.

Test cases:
  1. Parse detail HTML — all major fields extracted correctly
  2. Status mapping — Chinese → English conversion
  3. Study phase mapping — Chinese → English conversion
  4. Enrollment parsing — integer extraction from "国内: N ；" format
  5. Date parsing — Chinese date format conversion
  6. Multi-column table rows — <th>-<td> pairs correctly matched
  7. Eligibility criteria — inclusion + exclusion combined
  8. Normalise — NormalisedRecord produced correctly
  9. Cross-platform linking — CTR record linked to NCT master via entity resolution
 10. Identifier extraction — CTR numbers found in raw_payload
"""
from __future__ import annotations

import json
import os

import pytest

from db.connection import get_connection

# Path to a real CDE detail page fixture
_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_SAMPLE_HTML_PATH = os.path.join(_FIXTURE_DIR, "cde_detail_sample.html")


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def cde_detail_html() -> str:
    """Load the real CDE detail page HTML fixture."""
    with open(_SAMPLE_HTML_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def sources(conn):
    """Seed registry sources (NCT, CTR, ChiCTR, ICTRP)."""
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
    for label in ["Interventional", "Observational", "生物等效性试验/生物利用度试验"]:
        conn.execute("INSERT OR IGNORE INTO study_types (label) VALUES (?)", (label,))
    for label in ["Recruiting", "Completed", "Not yet recruiting", "Active, not recruiting"]:
        conn.execute("INSERT OR IGNORE INTO status_types (label) VALUES (?)", (label,))
    conn.commit()


@pytest.fixture
def conn(test_db):
    """Get connection to test database."""
    return get_connection()


# ── Helper ──────────────────────────────────────────────────────────────────


def _source_id(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


def _status_id(conn, label: str) -> int:
    return conn.execute(
        "SELECT status_type_id FROM status_types WHERE label = ?",
        (label,),
    ).fetchone()["status_type_id"]


def _insert_record(conn, source_short: str, source_trial_id: str,
                   title: str = "Test Trial",
                   raw_payload: str | None = None,
                   **kw) -> int:
    """Insert a registry_record and return its record_id."""
    sid = _source_id(conn, source_short)
    stid = _status_id(conn, "Recruiting")
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, raw_payload, is_latest)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (sid, source_trial_id, title, stid,
         raw_payload or json.dumps({"id": source_trial_id})),
    )
    conn.commit()
    return cur.lastrowid


# ── Tests ──────────────────────────────────────────────────────────────────


class TestParseDetailHtml:
    """Verify the HTML parser extracts all major fields correctly."""

    def test_source_trial_id(self, cde_detail_html):
        """登记号 → source_trial_id."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields["source_trial_id"] == "CTR20262758"

    def test_title(self, cde_detail_html):
        """试验通俗题目 → title."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("title")
        assert "SY-12321" in fields["title"] or "安全性" in fields["title"]

    def test_scientific_title(self, cde_detail_html):
        """试验专业题目 → scientific_title."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("scientific_title")
        assert "ALK" in fields["scientific_title"]

    def test_status_mapped(self, cde_detail_html):
        """试验状态 → mapped to English."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields["status"] == "Recruiting"

    def test_drug_name(self, cde_detail_html):
        """药物名称 → drug_name."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("drug_name")
        assert "SY-12321" in fields["drug_name"]

    def test_conditions(self, cde_detail_html):
        """适应症 → conditions."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("conditions")
        assert "非小细胞肺癌" in fields["conditions"] or "ALK" in fields["conditions"]

    def test_sponsors(self, cde_detail_html):
        """申请人名称 → sponsors."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("sponsors")
        assert "首药控股" in fields["sponsors"] or "北京" in fields["sponsors"]

    def test_protocol_number(self, cde_detail_html):
        """试验方案编号 → protocol_number."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("protocol_number") == "SY-12321-I-01"

    def test_enrollment_parsed(self, cde_detail_html):
        """目标入组人数 → integer."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert isinstance(fields.get("enrollment"), int)
        assert fields["enrollment"] == 60

    def test_registration_date(self, cde_detail_html):
        """首次公示信息日期 → parsed to ISO date."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("registration_date")
        assert fields["registration_date"].startswith("20")  # 2026-XX-XX

    def test_primary_endpoint(self, cde_detail_html):
        """主要终点指标及评价时间 → primary_endpoint."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("primary_endpoint")
        assert "TEAE" in fields["primary_endpoint"] or "指标" in fields["primary_endpoint"]

    def test_eligibility_criteria_combined(self, cde_detail_html):
        """入选标准 + 排除标准 → eligibility_criteria."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("eligibility_criteria")
        assert "Inclusion" in fields["eligibility_criteria"]
        assert "Exclusion" in fields["eligibility_criteria"]

    def test_study_type(self, cde_detail_html):
        """试验分类 → study_type."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("study_type")
        assert "安全性" in fields["study_type"] or "有效性" in fields["study_type"]

    def test_multi_column_table_rows(self, cde_detail_html):
        """Multi-column rows (<th><td><th><td> in one <tr>) are handled."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        # Both 登记号 and 试验状态 are in the same row
        assert fields.get("source_trial_id") == "CTR20262758"
        assert fields.get("status") == "Recruiting"

    def test_raw_html_length_recorded(self, cde_detail_html):
        """Raw HTML length is recorded for traceability."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("_raw_html_length", 0) > 30000

    def test_locations_extracted(self, cde_detail_html):
        """机构名称 → locations (参加机构)."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        fields = ChinaDrugTrialsCollector.parse_detail_html(cde_detail_html, "CTR20262758")
        assert fields.get("locations")
        assert "鼓楼医院" in fields["locations"] or "南京" in fields["locations"]


class TestNormalise:
    """Verify normalise() produces a valid NormalisedRecord."""

    def test_normalise_produces_record(self, cde_detail_html):
        """Normalise returns a NormalisedRecord with correct fields."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        from collectors.base import NormalisedRecord

        collector = ChinaDrugTrialsCollector()
        parsed = collector.parse_detail_html(cde_detail_html, "CTR20262758")
        raw = {
            "ctr_number": "CTR20262758",
            "uuid": "test-uuid-1234",
            "html": cde_detail_html,
            "parsed_fields": parsed,
        }
        nr = collector.normalise(raw)

        assert isinstance(nr, NormalisedRecord)
        assert nr.source_trial_id == "CTR20262758"
        assert nr.title
        assert nr.status == "Recruiting"
        assert nr.enrollment == 60
        assert nr.sponsors
        assert nr.interventions
        assert nr.conditions
        assert nr.raw_payload == cde_detail_html  # HTML stored as payload

    def test_normalise_no_crash_missing_fields(self):
        """Normalise handles empty/missing fields gracefully."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        collector = ChinaDrugTrialsCollector()
        raw = {
            "ctr_number": "CTR99999999",
            "uuid": "missing-uuid",
            "html": "<html><body>Empty</body></html>",
            "parsed_fields": {"source_trial_id": "CTR99999999"},
        }
        nr = collector.normalise(raw)
        assert nr.source_trial_id == "CTR99999999"
        assert nr.title == ""  # defaults to empty string
        assert nr.status is None


class TestLocalApiAdapter:
    """The self-hosted CTR API reuses parsing without mutating the queue."""

    def test_api_search_contract(self, monkeypatch):
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector

        collector = ChinaDrugTrialsCollector()
        monkeypatch.setattr(collector, "_choose_search_field",
                            lambda query: ("indication", "condition"))

        def fake_page(query, page, field=None):
            assert (query, page, field) == ("心力衰竭", 2, "indication")
            return [{
                "ctr": "CTR20262758",
                "title": "心力衰竭药物试验",
                "uuid": "uuid-2758",
                "index": "3",
            }]

        monkeypatch.setattr(collector, "_search_results_page", fake_page)
        payload = collector.api_search(" 心力衰竭 ", page=2)

        assert payload["source"] == "CTR"
        assert payload["query_field"] == "indication"
        assert payload["site_total"] is None
        assert payload["has_more"] is False
        assert payload["items"] == [{
            "source_trial_id": "CTR20262758",
            "title": "心力衰竭药物试验",
            "uuid": "uuid-2758",
            "index": "3",
            "url": (
                "https://www.chinadrugtrials.org.cn/"
                "clinicaltrials.searchlistdetail.dhtml?id=uuid-2758"
                "&ckm_index=3"
            ),
        }]

    def test_api_study_contract(self, monkeypatch, cde_detail_html):
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector

        collector = ChinaDrugTrialsCollector()
        monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
        monkeypatch.setattr(collector, "_lookup_by_regno", lambda trial_id: [{
            "ctr": trial_id,
            "title": "fixture",
            "uuid": "uuid-2758",
            "index": "3",
        }])
        monkeypatch.setattr(collector, "fetch_detail_page",
                            lambda uuid, index: cde_detail_html)

        payload = collector.api_study("ctr20262758")

        assert payload is not None
        assert payload["source_trial_id"] == "CTR20262758"
        assert payload["fields"]["enrollment"] == 60
        assert payload["canonical"]["conditions"]
        assert isinstance(payload["canonical"]["conditions"], list)
        assert "raw_payload" not in payload["canonical"]

    def test_api_study_returns_none_for_unknown_id(self, monkeypatch):
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector

        collector = ChinaDrugTrialsCollector()
        monkeypatch.setattr(collector, "_lookup_by_regno", lambda trial_id: [])
        assert collector.api_study("CTR20262758") is None


class TestHelperFunctions:
    """Test helper functions used by the collector."""

    def test_parse_int(self):
        """_parse_int extracts integer from Chinese text."""
        from collectors.chinadrugtrials import _parse_int
        assert _parse_int("国内: 60 ；") == 60
        assert _parse_int("24") == 24
        assert _parse_int("入组人数: 100人") == 100
        assert _parse_int(None) is None
        assert _parse_int("") is None
        assert _parse_int("登记人暂未填写") is None

    def test_parse_date(self):
        """_parse_date converts Chinese date formats to ISO."""
        from collectors.chinadrugtrials import _parse_date
        assert _parse_date("2026年07月17日") == "2026-07-17"
        assert _parse_date("2026-07-17") == "2026-07-17"
        assert _parse_date("2026/07/17") == "2026-07-17"
        assert _parse_date("2026.07.17") == "2026-07-17"
        assert _parse_date("2026年07月") == "2026-07-01"
        assert _parse_date(None) is None

    def test_has_cde_content(self):
        """_has_cde_content detects real vs WAF pages."""
        from collectors.chinadrugtrials import _has_cde_content
        # Real CDE page (must be >= 5000 bytes)
        real_html = "<html>" + ("药物临床试验登记与信息公示平台 CTR20262758 登记号 " * 200) + "</html>"
        assert len(real_html) >= 5000
        assert _has_cde_content(real_html)
        # WAF challenge (no CDE markers)
        waf_html = "<html>" + ("checking your browser before accessing " * 200) + "</html>"
        assert len(waf_html) >= 5000
        assert not _has_cde_content(waf_html)
        # Too short
        assert not _has_cde_content("<html>xx</html>")


class TestStatusPhaseMapping:
    """Test status and phase mapping logic."""

    def test_status_map_direct(self):
        """STATUS_MAP exact match works."""
        from collectors.chinadrugtrials import STATUS_MAP
        assert STATUS_MAP["进行中"] == "Recruiting"
        assert STATUS_MAP["已完成"] == "Completed"
        assert STATUS_MAP["尚未招募"] == "Not yet recruiting"
        assert STATUS_MAP["主动暂停"] == "Suspended"

    def test_status_map_partial(self):
        """STATUS_MAP partial match handles compound statuses."""
        from collectors.chinadrugtrials import STATUS_MAP
        # "进行中 尚未招募" - has 进行中 as substring
        raw = "进行中 尚未招募"
        mapped = None
        for cn, en in STATUS_MAP.items():
            if cn in raw:
                mapped = en
                break
        assert mapped == "Recruiting"
        # Ensure the first match wins: "进行中" before "尚未招募"

    def test_phase_map(self):
        """PHASE_MAP maps all known phase formats."""
        from collectors.chinadrugtrials import PHASE_MAP
        assert PHASE_MAP["Ⅰ期"] == "Phase 1"
        assert PHASE_MAP["III期"] == "Phase 3"
        assert PHASE_MAP["Ⅰ/Ⅱ期"] == "Phase 1/Phase 2"
        assert PHASE_MAP["其它"] == "Other"


class TestCrossPlatformLinking:
    """Test CTR ↔ NCT cross-platform linking via entity resolution."""

    def test_ctr_cross_ref_nct_linked(self, conn, sources, lookup_types):
        """CTR record with NCT cross-ref in raw_payload → linked to same master."""
        from core.entity_resolution import resolve_record

        # Step 1: Insert NCT record
        rid_nct = _insert_record(conn, "NCT", "NCT04760888",
                                  title="Global Vaccine Trial")

        # Step 2: Insert CTR record with NCT number in raw_payload HTML
        ctr_raw_payload = (
            '<html><body>'
            '<th>相关登记号</th><td>NCT04760888</td>'
            '<th>登记号</th><td>CTR20250001</td>'
            '</body></html>'
        )
        rid_ctr = _insert_record(conn, "CTR", "CTR20250001",
                                  title="Global Vaccine Trial (China)",
                                  raw_payload=ctr_raw_payload)

        # Step 3: Resolve NCT → creates new master
        r_nct = resolve_record(rid_nct)
        assert r_nct["action"] == "created"

        # Step 4: Resolve CTR → should find NCT cross-ref → AUTO_CONFIRMED
        r_ctr = resolve_record(rid_ctr)
        assert r_ctr["action"] == "linked", f"Expected linked, got {r_ctr}"
        assert r_ctr["match_status"] == "AUTO_CONFIRMED"
        assert r_ctr["master_trial_id"] == r_nct["master_trial_id"]

    def test_ctr_no_cross_ref_creates_separate(self, conn, sources, lookup_types):
        """CTR record with no cross-ref → separate master trial."""
        from core.entity_resolution import resolve_record

        rid_nct = _insert_record(conn, "NCT", "NCT99999999",
                                  title="Completely Different Trial")
        rid_ctr = _insert_record(conn, "CTR", "CTR20999999",
                                  title="分开的中国试验",
                                  raw_payload="<html>CTR20999999 only</html>")

        r_nct = resolve_record(rid_nct)
        r_ctr = resolve_record(rid_ctr)
        assert r_nct["action"] == "created"
        assert r_ctr["action"] == "created"  # separate master
        assert r_nct["master_trial_id"] != r_ctr["master_trial_id"]

    def test_identifier_extraction_from_ctr_payload(self, conn, sources, lookup_types):
        """CTR numbers in raw_payload are extracted as identifiers."""
        from core.identifier_extraction import extract_and_save_identifiers

        html_payload = (
            '<html><th>登记号</th><td>CTR20251234</td>'
            '<th>相关登记号</th><td>NCT01234567</td></html>'
        )
        rid = _insert_record(conn, "CTR", "CTR20251234",
                              raw_payload=html_payload)

        ids = extract_and_save_identifiers(rid)
        id_types = {i["identifier_type"] for i in ids}
        assert "CTR" in id_types, f"CTR type not found in {id_types}"
        ctr_ids = [i for i in ids if i["identifier_type"] == "CTR"]
        assert ctr_ids[0]["identifier_value"] == "CTR20251234"
        # The NCT should also be found in the HTML
        nct_ids = [i for i in ids if i["identifier_type"] == "NCT"]
        if nct_ids:
            assert nct_ids[0]["identifier_value"] == "NCT01234567"


class TestIntegration:
    """Integration-level tests combining CDE collector + DB pipeline."""

    def test_full_pipeline_with_real_html(self, conn, sources, lookup_types,
                                           cde_detail_html):
        """Run the full normalise→upsert pipeline with real CDE HTML."""
        from collectors.chinadrugtrials import ChinaDrugTrialsCollector
        
        collector = ChinaDrugTrialsCollector()
        parsed = collector.parse_detail_html(cde_detail_html, "CTR20262758")
        raw = {
            "ctr_number": "CTR20262758",
            "uuid": "uuid-placeholder",
            "html": cde_detail_html,
            "parsed_fields": parsed,
        }
        nr = collector.normalise(raw)

        # Verify all key fields for the DB upsert pipeline
        assert nr.source_trial_id == "CTR20262758"
        assert nr.title
        assert nr.status == "Recruiting"
        assert nr.enrollment == 60
        assert nr.compute_hash()

        # Run the BaseCollector _upsert_record to persist
        # (need to inject source_id manually since DB has CTR source)
        conn.execute(
            "INSERT OR IGNORE INTO sync_status (source_id) VALUES (?)",
            (_source_id(conn, "CTR"),),
        )
        conn.commit()

        result = collector._upsert_record(nr, is_bootstrap=True)
        assert result["action"] in ("new", "updated", "skipped")
        assert result["record_id"] is not None

        # Verify the record was stored
        row = conn.execute(
            "SELECT source_trial_id, title, enrollment, status_id FROM registry_records WHERE record_id = ?",
            (result["record_id"],),
        ).fetchone()
        assert row is not None
        assert row["source_trial_id"] == "CTR20262758"
        assert row["enrollment"] == 60


# ── live_search 检索字段路由（2026-09-24 indication 接线） ─────────────────


class TestLiveSearchFieldRouting:
    """CJK 实时检索词走 indication（适应症）字段，其余维持 keywords。

    背景：站内通用 keywords 检索对疾病词不可靠（单字被忽略返回默认
    列表、"心肌炎"恒 0），适应症字段才按病名过滤。全部离线：
    monkeypatch _fetch_page 捕获 URL，空结果页避免触碰入队路径。
    """

    EMPTY_PAGE = ("<html><body><table class='searchTable'>"
                  "<tr><th>序号</th><th>登记号</th></tr></table></body></html>")

    @pytest.fixture
    def url_capture(self, monkeypatch, conn):
        """零延迟采集器 + 捕获 _fetch_page 收到的 URL。"""
        from urllib.parse import parse_qs, urlparse

        from collectors.chinadrugtrials import ChinaDrugTrialsCollector

        collector = ChinaDrugTrialsCollector()
        monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
        urls: list = []

        def fake_fetch_page(url, timeout_ms=None):
            urls.append(url)
            return self.EMPTY_PAGE

        monkeypatch.setattr(collector, "_fetch_page", fake_fetch_page)
        collector._urls = urls
        collector._qs = lambda u: parse_qs(urlparse(u).query)
        return collector

    def test_default_and_post_live_state_is_keywords(self, url_capture):
        """无 live_search 栈时（发现走查/登记号反查）恒走 keywords。"""
        url_capture._search_results_page("心", 1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["keywords"] == ["心"]
        assert "indication" not in qs and "secondLevel" not in qs

    def test_live_search_cjk_routes_to_indication(self, url_capture):
        """live_search 中文词 → secondLevel=1 + indication 字段。"""
        stats = url_capture.live_search("心力衰竭", max_pages=1)
        assert stats["found"] == 0  # 空页：不触碰入队，仅看路由
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["indication"] == ["心力衰竭"]
        assert qs["secondLevel"] == ["1"]
        assert qs["currentpage"] == ["1"]
        assert "keywords" not in qs

    def test_live_search_ascii_keeps_keywords(self, url_capture):
        """英文/登记号类 ASCII 词维持 keywords 字段（CTR 无英文适应症）。"""
        url_capture.live_search("CTR20263631", max_pages=1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["keywords"] == ["CTR20263631"]
        assert "indication" not in qs

    def test_field_restored_after_live_search(self, url_capture):
        """live_search 结束后字段选择复位 keywords，不影响后续调用。"""
        url_capture.live_search("心肌炎", max_pages=1)
        url_capture._urls.clear()
        url_capture._search_results_page("糖尿病", 1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["keywords"] == ["糖尿病"]

    def test_indication_url_paginates(self, url_capture):
        """indication 检索的 currentpage 翻页参数正确传递。"""
        url_capture._live_tls.field = "indication"
        try:
            url_capture._search_results_page("心力衰竭", 2)
        finally:
            url_capture._live_tls.field = "keywords"
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["currentpage"] == ["2"]
        assert qs["indication"] == ["心力衰竭"]

    # ── 2026-09-29 升级：按词义路由（申办者/疾病/未知词），替代一刀切 ──

    def test_live_search_sponsor_marker_word_routes_to_keywords(
            self, url_capture):
        """带机构特征词的申办者（上海罗氏制药有限公司）→ keywords 通用框。

        keywords 匹配申办者类字段（2026-09-29 实测 keywords=罗氏 → 240
        条罗氏试验）；走 indication 必 0 命中，且不触发 indication 重试
        （sponsor 词重试纯属浪费请求）。
        """
        stats = url_capture.live_search("上海罗氏制药有限公司", max_pages=1)
        assert len(url_capture._urls) == 1  # 申办者词不做 indication 重试
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["keywords"] == ["上海罗氏制药有限公司"]
        assert "indication" not in qs
        assert stats["field_used"] == "keywords"

    def test_live_search_latin_sponsor_marker_routes_to_keywords(
            self, url_capture):
        """英文公司名（Roche Diagnostics GmbH）维持 keywords。"""
        url_capture.live_search("Roche Diagnostics GmbH", max_pages=1)
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["keywords"] == ["Roche Diagnostics GmbH"]
        assert "indication" not in qs

    def test_live_search_library_sponsor_bare_word_routes_to_keywords(
            self, url_capture, conn):
        """无特征词的申办者简称（罗氏）靠本地库 sponsors 证据识别。

        本地 CTR 记录的申办者是罗氏(中国)投资有限公司 → 「罗氏」是申办
        者词 → keywords（2026-09-29 用户案例：此前走 indication 恒 0）。
        """
        rid = _insert_record(conn, "CTR", "CTR20269998", title="罗氏注册试验")
        conn.execute(
            "UPDATE registry_records SET sponsors = ? WHERE record_id = ?",
            ('["罗氏(中国)投资有限公司"]', rid))
        conn.commit()
        stats = url_capture.live_search("罗氏", max_pages=1)
        assert len(url_capture._urls) == 1
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["keywords"] == ["罗氏"]
        assert "indication" not in qs
        assert stats["field_used"] == "keywords"

    def test_live_search_condition_term_keeps_indication_single_request(
            self, url_capture):
        """术语表疾病词（心力衰竭）→ indication 直达，无额外重试请求。"""
        stats = url_capture.live_search("心力衰竭", max_pages=1)
        assert len(url_capture._urls) == 1
        assert url_capture._qs(url_capture._urls[0])["indication"] == ["心力衰竭"]
        assert stats["field_used"] == "indication"

    def test_live_search_single_char_cjk_routes_to_indication(
            self, url_capture):
        """单字 CJK 恒走 indication（keywords 会返回未过滤默认列表假阳性）。"""
        url_capture.live_search("心", max_pages=1)
        assert len(url_capture._urls) == 1
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["indication"] == ["心"]

    def test_live_search_unknown_word_retries_indication_on_miss(
            self, url_capture):
        """未知多字词：keywords 0 命中（完整跑完）→ indication 重试一次。"""
        stats = url_capture.live_search("某某未知词", max_pages=1)
        assert len(url_capture._urls) == 2
        assert url_capture._qs(url_capture._urls[0])["keywords"] == ["某某未知词"]
        retry_qs = url_capture._qs(url_capture._urls[1])
        assert retry_qs["indication"] == ["某某未知词"]
        # 重试也未命中 → 返回原 keywords 统计（field_used 如实标注）
        assert stats["found"] == 0
        assert stats["field_used"] == "keywords"

    def test_live_search_unknown_word_no_retry_when_keywords_hit(
            self, url_capture):
        """未知词 keywords 有命中 → 不做 indication 重试。"""

        def fake_fetch_page(url, timeout_ms=None):
            url_capture._urls.append(url)
            return ("<html><body><table class='searchTable'>"
                    "<tr><th>序号</th><th>登记号</th></tr>"
                    "<tr><td>1</td><td><a id='uuid-1'>CTR20263631</a></td>"
                    "<td>进行中</td><td>某某未知词胶囊</td><td>无</td>"
                    "<td>标题</td></tr></table></body></html>")

        url_capture._fetch_page = fake_fetch_page
        stats = url_capture.live_search("某某未知词", max_pages=1)
        assert len(url_capture._urls) == 1
        assert stats["found"] == 1
        assert stats["field_used"] == "keywords"

    def test_live_search_resumed_field_hint_skips_reclassification(
            self, url_capture):
        """续批 field 提示：沿用首查生效字段，不重新分类、不重试。"""
        stats = url_capture.live_search("某某未知词", max_pages=1,
                                        field="indication")
        assert len(url_capture._urls) == 1
        qs = url_capture._qs(url_capture._urls[0])
        assert qs["indication"] == ["某某未知词"]
        assert stats["field_used"] == "indication"

    def test_live_search_no_indication_retry_after_page_failure(
            self, url_capture, monkeypatch):
        """keywords 路径页失败（WAF 抖动）→ 不叠加 indication 重试。"""

        def failing_fetch_page(url, timeout_ms=None):
            url_capture._urls.append(url)
            raise RuntimeError("WAF blocked search page")

        monkeypatch.setattr(url_capture, "_fetch_page", failing_fetch_page)
        stats = url_capture.live_search("某某未知词", max_pages=1)
        assert len(url_capture._urls) == 1
        assert stats["stopped_reason"] == "page_failed"
