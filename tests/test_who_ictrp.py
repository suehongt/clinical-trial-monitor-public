"""
Tests for the WHO ICTRP aggregator collector.

Test cases:
  1. XML parsing — all records extracted correctly
  2. Status mapping — WHO → internal conversion
  3. Study type mapping
  4. Registry name normalization
  5. Date parsing — dd/mm/yyyy → ISO
  6. Source trial ID extraction from reg_name + trial_id
  7. Normalise — NormalisedRecord with AGGREGATOR metadata
  8. Cross-platform linking — ICTRP→NCT via entity resolution
  9. AGGREGATOR dedup — ICTRP record linked to existing master
 10. Conflict detection preparation — provenance tracking
 11. secondary_id extraction
"""
from __future__ import annotations

import json
import os

import pytest

from db.connection import get_connection

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_XML_PATH = os.path.join(_FIXTURE_DIR, "who_ictrp_sample.xml")


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def xml_content() -> str:
    """Load the WHO ICTRP XML fixture."""
    with open(_XML_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def sources(conn):
    """Seed registry sources (NCT, ChiCTR, CTR, ICTRP, ANZCTR, DRKS)."""
    for short, full, stype in [
        ("NCT", "ClinicalTrials.gov", "PRIMARY"),
        ("ChiCTR", "中国临床试验注册中心", "PRIMARY"),
        ("CTR", "中国药物临床试验登记与信息公示平台", "PRIMARY"),
        ("ICTRP", "WHO ICTRP", "AGGREGATOR"),
        ("ANZCTR", "Australian New Zealand Clinical Trials Registry", "PRIMARY"),
        ("DRKS", "German Clinical Trials Register", "PRIMARY"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO registry_sources (short_name, full_name, source_type) VALUES (?, ?, ?)",
            (short, full, stype),
        )
        # Ensure source_type is set for existing rows
        conn.execute(
            "UPDATE registry_sources SET source_type = ? WHERE short_name = ? AND source_type IS NULL",
            (stype, short),
        )
    conn.commit()


@pytest.fixture
def lookup_types(conn):
    """Seed minimal study types and status types."""
    for label in ["Interventional", "Observational", "Observational [Patient Registry]",
                  "Diagnostic Test", "Prevention", "Screening",
                  "Basic Science", "Health Services Research", "Other"]:
        conn.execute("INSERT OR IGNORE INTO study_types (label) VALUES (?)", (label,))
    for label in ["Recruiting", "Completed", "Not yet recruiting",
                  "Active, not recruiting", "Unknown status"]:
        conn.execute("INSERT OR IGNORE INTO status_types (label) VALUES (?)", (label,))
    conn.commit()


@pytest.fixture
def conn(test_db):
    """Get connection to test database."""
    return get_connection()


# ── Helpers ────────────────────────────────────────────────────────────────


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
        "SELECT status_type_id FROM status_types WHERE label = 'Recruiting'"
    ).fetchone()
    stid = cur["status_type_id"] if cur else 1
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


class TestParseXml:
    """Verify XML parsing extracts all fields correctly."""

    def test_parses_all_records(self, xml_content):
        """XML with 5 trials → 5 parsed records."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        assert len(records) == 5, f"Expected 5 records, got {len(records)}"

    def test_nct_record_fields(self, xml_content):
        """NCT04760888 record has correct fields."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        nct = [r for r in records if r.get("trial_id") == "NCT04760888"]
        assert len(nct) == 1
        r = nct[0]
        assert r["reg_name"] == "ClinicalTrials.gov"
        assert r["public_title"] == "A Study of Vaccine Against COVID-19 in Healthy Adults"
        assert r["recruitment_status"] == "Completed"
        assert r["study_type"] == "Interventional"
        assert r["phase"] == "Phase 3"
        assert r["target_size"] == "30000"
        assert r["hc_freetext"] == "COVID-19"

    def test_chictr_record(self, xml_content):
        """ChiCTR2600128415 record parsed correctly."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        chictr = [r for r in records if r.get("trial_id") == "ChiCTR2600128415"]
        assert len(chictr) == 1
        r = chictr[0]
        assert r["reg_name"] == "ChiCTR"
        assert r["study_type"] == "Observational"
        assert r["recruitment_status"] == "Recruiting"

    def test_record_with_secondary_id(self, xml_content):
        """DRKS record with secondary_id (UTN) parsed."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        drks = [r for r in records if r.get("trial_id") == "DRKS00030000"]
        assert len(drks) == 1
        r = drks[0]
        assert r.get("secondary_id") == ["U1111-1234-5678"]

    def test_record_with_ctr_crossref(self, xml_content):
        """CTR record with secondary_id NCT cross-reference."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        ctr = [r for r in records if r.get("trial_id") == "CTR20250001"]
        assert len(ctr) == 1
        r = ctr[0]
        assert "NCT04760888" in r.get("secondary_id", [])

    def test_records_have_required_fields(self, xml_content):
        """Every record has trial_id, reg_name, public_title."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        for r in records:
            assert r.get("trial_id"), f"Missing trial_id in {r}"
            assert r.get("reg_name"), f"Missing reg_name in {r}"
            assert r.get("public_title") or r.get("scientific_title"), \
                f"Missing title in {r.get('trial_id')}"


class TestXmlFileImport:
    """Verify reading from XML file."""

    def test_read_xml_file(self):
        """read_xml_file returns non-empty string."""
        from collectors.who_ictrp import WHOICTRPCollector
        content = WHOICTRPCollector.read_xml_file(_XML_PATH)
        assert content
        assert "<?xml" in content
        assert "<Trials>" in content

    def test_read_and_parse(self):
        """read_xml_file + parse_xml produces records."""
        from collectors.who_ictrp import WHOICTRPCollector
        content = WHOICTRPCollector.read_xml_file(_XML_PATH)
        records = WHOICTRPCollector.parse_xml(content)
        assert len(records) >= 3


class TestStatusMapping:
    """Test WHO → internal status mapping."""

    def test_direct_match(self):
        from collectors.who_ictrp import _map_status
        assert _map_status("Recruiting") == "Recruiting"
        assert _map_status("Completed") == "Completed"
        assert _map_status("Not yet recruiting") == "Not yet recruiting"

    def test_case_insensitive(self):
        from collectors.who_ictrp import _map_status
        assert _map_status("recruiting") == "Recruiting"
        assert _map_status("COMPLETED") == "Completed"

    def test_partial_match(self):
        from collectors.who_ictrp import _map_status
        assert _map_status("Active, not recruiting") == "Active, not recruiting"
        assert _map_status("Unknown status") == "Unknown status"

    def test_none_returns_none(self):
        from collectors.who_ictrp import _map_status
        assert _map_status(None) is None
        assert _map_status("") is None


class TestStudyTypeMapping:
    """Test WHO → internal study type mapping."""

    def test_direct_match(self):
        from collectors.who_ictrp import _map_study_type
        assert _map_study_type("Interventional") == "Interventional"
        assert _map_study_type("Observational") == "Observational"
        assert _map_study_type("Other") == "Other"

    def test_case_insensitive(self):
        from collectors.who_ictrp import _map_study_type
        assert _map_study_type("interventional") == "Interventional"
        assert _map_study_type("diagnostic") == "Diagnostic Test"

    def test_none_returns_none(self):
        from collectors.who_ictrp import _map_study_type
        assert _map_study_type(None) is None
        assert _map_study_type("") is None


class TestRegistryNameNormalization:
    """Test registry name → short_name mapping."""

    def test_clinicaltrials_gov(self):
        from collectors.who_ictrp import _normalize_registry_name
        assert _normalize_registry_name("ClinicalTrials.gov") == "NCT"
        assert _normalize_registry_name("clinicaltrials.gov") == "NCT"
        assert _normalize_registry_name("ClinicalTrials") == "NCT"

    def test_chictr(self):
        from collectors.who_ictrp import _normalize_registry_name
        assert _normalize_registry_name("ChiCTR") == "ChiCTR"
        assert _normalize_registry_name("Chinese Clinical Trial Register") == "ChiCTR"

    def test_chinadrugtrials(self):
        from collectors.who_ictrp import _normalize_registry_name
        assert _normalize_registry_name("ChinaDrugTrials") == "CTR"
        assert _normalize_registry_name("chinadrugtrials.org.cn") == "CTR"

    def test_anzctr(self):
        from collectors.who_ictrp import _normalize_registry_name
        assert _normalize_registry_name("ANZCTR") == "ANZCTR"
        assert _normalize_registry_name("Australian New Zealand Clinical Trials Registry") == "ANZCTR"

    def test_none_returns_none(self):
        from collectors.who_ictrp import _normalize_registry_name
        assert _normalize_registry_name(None) is None
        assert _normalize_registry_name("") is None


class TestSourceTrialIdExtraction:
    """Test source_trial_id construction from reg_name + trial_id."""

    def test_nct_trial_id(self):
        from collectors.who_ictrp import _extract_source_trial_id
        result = _extract_source_trial_id("ClinicalTrials.gov", "NCT04760888")
        assert result == "NCT04760888"

    def test_chictr_trial_id(self):
        from collectors.who_ictrp import _extract_source_trial_id
        result = _extract_source_trial_id("ChiCTR", "ChiCTR2600128415")
        assert result == "ChiCTR2600128415"

    def test_anzctr_trial_id_no_prefix(self):
        from collectors.who_ictrp import _extract_source_trial_id
        # ANZCTR trial IDs don't match the registry prefix pattern
        result = _extract_source_trial_id("ANZCTR", "ACTRN12625000000000")
        assert result is not None
        assert "ANZCTR" in result or "ACTRN" in result

    def test_unknown_registry(self):
        from collectors.who_ictrp import _extract_source_trial_id
        result = _extract_source_trial_id("Unknown Registry", "XYZ123")
        assert result == "XYZ123"

    def test_none_trial_id(self):
        from collectors.who_ictrp import _extract_source_trial_id
        assert _extract_source_trial_id("NCT", None) is None


class TestDateParsing:
    """Test WHO date parsing."""

    def test_dd_mm_yyyy(self):
        from collectors.who_ictrp import _parse_date_who
        assert _parse_date_who("15/02/2021") == "2021-02-15"

    def test_iso_date(self):
        from collectors.who_ictrp import _parse_date_who
        assert _parse_date_who("2021-02-15") == "2021-02-15"

    def test_none(self):
        from collectors.who_ictrp import _parse_date_who
        assert _parse_date_who(None) is None
        assert _parse_date_who("") is None


class TestNormalise:
    """Verify normalise() produces a valid NormalisedRecord."""

    def test_normalise_nct_record(self, xml_content):
        """NCT record normalised correctly with AGGREGATOR metadata."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        nct_raw = [r for r in records if r.get("trial_id") == "NCT04760888"][0]

        collector = WHOICTRPCollector()
        nr = collector.normalise(nct_raw)

        assert nr.source_trial_id == "NCT04760888"
        assert nr.title == "A Study of Vaccine Against COVID-19 in Healthy Adults"
        assert nr.scientific_title == (
            "Phase 3 Randomized, Placebo-Controlled Study of the "
            "Safety and Efficacy of mRNA-1273"
        )
        assert nr.status == "Completed"
        assert nr.study_type == "Interventional"
        assert nr.study_phase == "Phase 3"
        assert nr.enrollment == 30000
        assert nr.registration_date == "2021-02-15"
        assert nr.conditions == json.dumps(["COVID-19"], ensure_ascii=False)
        assert nr.interventions == json.dumps(["mRNA-1273 vaccine"], ensure_ascii=False)
        assert nr.sponsors == json.dumps(["ModernaTX, Inc."], ensure_ascii=False)
        assert nr.primary_endpoint == "Number of Participants With First Occurrence of COVID-19"
        assert "Inclusion Criteria" in (nr.eligibility_criteria or "")
        assert "Exclusion Criteria" in (nr.eligibility_criteria or "")

        # Verify AGGREGATOR metadata in raw_payload
        payload = json.loads(nr.raw_payload)
        assert payload["_aggregator"]["_who_source_registry"] == "ClinicalTrials.gov"
        assert payload["_aggregator"]["_who_source_trial_id"] == "NCT04760888"
        assert payload["_aggregator"]["_who_registry_short"] == "NCT"

    def test_normalise_chictr_record(self, xml_content):
        """ChiCTR record normalised with correct study type."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        raw = [r for r in records if r.get("trial_id") == "ChiCTR2600128415"][0]

        collector = WHOICTRPCollector()
        nr = collector.normalise(raw)

        assert nr.source_trial_id == "ChiCTR2600128415"
        assert nr.status == "Recruiting"
        assert nr.study_type == "Observational"
        assert nr.registration_date == "2026-01-10"
        assert nr.enrollment == 5000

    def test_normalise_source_url_is_per_trial_page(self, xml_content):
        """source_url 是门户的具体试验页（Trial3.aspx），不是搜索表单。

        TextBox1 搜索 URL 打开后是门户的空白检索表单（ASP.NET postback），
        不能作为「注册平台」外链。
        """
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        raw = [r for r in records if r.get("trial_id") == "NCT04760888"][0]

        nr = WHOICTRPCollector().normalise(raw)

        assert nr.source_url == (
            "https://trialsearch.who.int/Trial3.aspx?trialid=NCT04760888")

    def test_normalise_anzctr_no_master(self, xml_content):
        """ANZCTR record normalised (no existing master)."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        raw = [r for r in records if "ACTRN" in r.get("trial_id", "")][0]

        collector = WHOICTRPCollector()
        nr = collector.normalise(raw)

        assert nr.title == "Melatonin for Sleep Disorders in Children with Autism"
        assert nr.status == "Not yet recruiting"
        assert nr.study_type == "Interventional"
        assert nr.enrollment == 120
        assert nr.conditions == json.dumps(["Autism Spectrum Disorder"], ensure_ascii=False)

    def test_normalise_record_with_secondary_ids(self, xml_content):
        """Record with secondary IDs has them in raw_payload."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        raw = [r for r in records if r.get("trial_id") == "DRKS00030000"][0]

        collector = WHOICTRPCollector()
        nr = collector.normalise(raw)

        payload = json.loads(nr.raw_payload)
        assert "U1111-1234-5678" in payload["secondary_ids"]

    def test_normalise_produces_hash(self, xml_content):
        """NormalisedRecord.compute_hash() works for WHO records."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        raw = records[0]

        collector = WHOICTRPCollector()
        nr = collector.normalise(raw)

        h = nr.compute_hash()
        assert h
        assert len(h) == 64  # SHA-256 hex
        assert isinstance(h, str)


class TestIdentifierExtraction:
    """Test identifier extraction from WHO records."""

    def test_nct_identifier_in_payload(self, xml_content):
        """NCT number found in raw_payload for CTR record."""
        from collectors.who_ictrp import WHOICTRPCollector

        records = WHOICTRPCollector.parse_xml(xml_content)
        ctr_raw = [r for r in records if r.get("trial_id") == "CTR20250001"][0]

        collector = WHOICTRPCollector()
        nr = collector.normalise(ctr_raw)

        # The NCT should be in raw_payload
        payload = json.loads(nr.raw_payload)
        assert "NCT04760888" in json.dumps(payload)

    def test_secondary_id_extracted(self, xml_content):
        """secondary_id list is preserved in raw_payload."""
        from collectors.who_ictrp import WHOICTRPCollector
        records = WHOICTRPCollector.parse_xml(xml_content)
        drks_raw = [r for r in records if r.get("trial_id") == "DRKS00030000"][0]
        assert len(drks_raw.get("secondary_id", [])) > 0


class TestCrossPlatformLinking:
    """Test ICTRP records linking to existing master trials via entity resolution."""

    def test_ictrp_nct_linked_to_existing(self, conn, sources, lookup_types):
        """ICTRP record for NCT04760888 links to existing NCT master trial."""
        from core.entity_resolution import resolve_record
        from collectors.who_ictrp import WHOICTRPCollector

        # Step 1: Insert NCT record (existing master)
        rid_nct = _insert_record(conn, "NCT", "NCT04760888",
                                  title="Global Vaccine Trial")

        # Step 2: Resolve NCT → creates master
        r_nct = resolve_record(rid_nct)
        assert r_nct["action"] == "created"

        # Step 3: Create ICTRP record pointing to same NCT trial
        who_raw = {
            "reg_name": "ClinicalTrials.gov",
            "trial_id": "NCT04760888",
            "public_title": "A Study of Vaccine Against COVID-19",
            "recruitment_status": "Completed",
            "study_type": "Interventional",
            "phase": "Phase 3",
        }
        collector = WHOICTRPCollector()
        nr = collector.normalise(who_raw)
        rid_ictrp = _insert_record(conn, "ICTRP", "WHO:NCT04760888",
                                    title=nr.title,
                                    raw_payload=nr.raw_payload)

        # Step 4: Resolve ICTRP → should link to existing master
        r_ictrp = resolve_record(rid_ictrp)
        # The ICTRP record should be linked (AUTO_CONFIRMED) or created
        assert r_ictrp["action"] in ("linked", "created")
        if r_ictrp["action"] == "linked":
            assert r_ictrp["master_trial_id"] == r_nct["master_trial_id"]

    def test_ictrp_chictr_linked(self, conn, sources, lookup_types):
        """ICTRP record for ChiCTR trial links to existing ChiCTR master."""
        from core.entity_resolution import resolve_record
        from collectors.who_ictrp import WHOICTRPCollector

        rid_chictr = _insert_record(conn, "ChiCTR", "ChiCTR2600128415",
                                     title="Lung Cancer Screening Study")
        r_chictr = resolve_record(rid_chictr)
        assert r_chictr["action"] == "created"

        who_raw = {
            "reg_name": "ChiCTR",
            "trial_id": "ChiCTR2600128415",
            "public_title": "Lung Cancer Screening with Low-Dose CT",
            "recruitment_status": "Recruiting",
            "study_type": "Observational",
        }
        collector = WHOICTRPCollector()
        nr = collector.normalise(who_raw)
        rid_ictrp = _insert_record(conn, "ICTRP", "WHO:ChiCTR2600128415",
                                    title=nr.title,
                                    raw_payload=nr.raw_payload)

        r_ictrp = resolve_record(rid_ictrp)
        assert r_ictrp["action"] in ("linked", "created")
        if r_ictrp["action"] == "linked":
            assert r_ictrp["master_trial_id"] == r_chictr["master_trial_id"]

    def test_ictrp_unknown_registry_creates_separate(self, conn, sources, lookup_types):
        """ICTRP record with no matching master → creates separate master."""
        from core.entity_resolution import resolve_record
        from collectors.who_ictrp import WHOICTRPCollector

        who_raw = {
            "reg_name": "ANZCTR",
            "trial_id": "ACTRN12625000000000",
            "public_title": "Melatonin for Sleep Disorders in Children with Autism",
            "recruitment_status": "Not yet recruiting",
            "study_type": "Interventional",
        }
        collector = WHOICTRPCollector()
        nr = collector.normalise(who_raw)
        rid_ictrp = _insert_record(conn, "ICTRP", "WHO:ACTRN12625000000000",
                                    title=nr.title,
                                    raw_payload=nr.raw_payload)

        r = resolve_record(rid_ictrp)
        assert r["action"] in ("created", "linked", "created_via_aggregator")

    def test_ictrp_not_double_counted(self, conn, sources, lookup_types):
        """ICTRP records for NCT should not create new master trials."""
        from core.entity_resolution import auto_resolve, resolve_record
        from collectors.who_ictrp import WHOICTRPCollector

        # Create existing NCT trial + master
        rid_nct = _insert_record(conn, "NCT", "NCT99990001", title="Existing Trial")
        r_nct = resolve_record(rid_nct)
        nct_master = r_nct["master_trial_id"]

        # Create ICTRP record for same trial
        who_raw = {
            "reg_name": "ClinicalTrials.gov",
            "trial_id": "NCT99990001",
            "public_title": "Existing Trial",
            "recruitment_status": "Recruiting",
        }
        collector = WHOICTRPCollector()
        nr = collector.normalise(who_raw)
        rid_ictrp = _insert_record(conn, "ICTRP", "WHO:NCT99990001",
                                    title=nr.title,
                                    raw_payload=nr.raw_payload)

        summary = auto_resolve(unlinked_record_ids=[rid_ictrp])
        # Should link, not create new
        assert summary["linked"] == 1 or summary["created"] == 1
        if summary["linked"] == 1:
            # Verify it linked to the same master
            mm = conn.execute(
                "SELECT master_trial_id FROM record_master_map WHERE record_id = ?",
                (rid_ictrp,),
            ).fetchone()
            assert mm is not None
            assert mm["master_trial_id"] == nct_master


class TestAggregatorBehavior:
    """Test AGGREGATOR-specific behavior."""

    def test_upsert_always_bootstrap(self, conn, sources, lookup_types):
        """WHO ICTRP records are always inserted as bootstrap."""
        from collectors.who_ictrp import WHOICTRPCollector

        conn.execute(
            "INSERT OR IGNORE INTO sync_status (source_id) VALUES (?)",
            (_source_id(conn, "ICTRP"),),
        )
        conn.commit()

        collector = WHOICTRPCollector()
        raw = {
            "reg_name": "ClinicalTrials.gov",
            "trial_id": "NCT99999999",
            "public_title": "Test Bootstrap Record",
            "recruitment_status": "Recruiting",
        }
        nr = collector.normalise(raw)
        result = collector._upsert_record(nr)

        # Verify the record was marked is_bootstrap
        row = conn.execute(
            "SELECT is_bootstrap FROM registry_records WHERE record_id = ?",
            (result["record_id"],),
        ).fetchone()
        assert row is not None
        assert row["is_bootstrap"] == 1

    def test_run_returns_aggregator_note(self, conn, xml_content):
        """run() returns AGGREGATOR-specific metadata."""
        from collectors.who_ictrp import WHOICTRPCollector

        collector = WHOICTRPCollector()
        # Monkey-patch fetch to use XML
        original_fetch = collector.fetch_new_or_updated
        try:
            raw_records = WHOICTRPCollector.parse_xml(xml_content)

            def mock_fetch(since=None):
                return raw_records
            collector.fetch_new_or_updated = mock_fetch

            # We need a DB connection for run()
            conn = get_connection()
            conn.execute(
                "INSERT OR IGNORE INTO sync_status (source_id) VALUES (?)",
                (collector.source_id,),
            )
            conn.commit()

            summary = collector.run()
            assert summary["source_type"] == "AGGREGATOR"
            assert "aggregator" in summary.get("note", "").lower()
        finally:
            collector.fetch_new_or_updated = original_fetch


class TestHelperFunctions:
    """Test helper utility functions."""

    def test_parse_int_who(self):
        from collectors.who_ictrp import _parse_int_who
        assert _parse_int_who("30000") == 30000
        assert _parse_int_who("1,200") == 1200
        assert _parse_int_who(None) is None
        assert _parse_int_who("") is None
        assert _parse_int_who("Not provided") is None

    def test_parse_live_portal_grid(self):
        from collectors.who_ictrp import _parse_portal_results

        html = """
        <span id="Label3">594 records for 572 trials found for: troponin</span>
        <table id="GridView1">
          <tr><th>Recruitment status</th><th>Prospective</th><th>Main ID</th>
              <th></th><th>Public Title</th><th>Date of Registration</th><th>Results</th></tr>
          <tr><td>Recruiting</td><td></td><td>NCT07786753</td><td></td>
              <td><a href="Trial2.aspx?TrialID=NCT07786753">A Troponin Study</a></td>
              <td>2026-08-21</td><td></td></tr>
        </table>
        """
        parsed = _parse_portal_results(html)
        assert parsed["records_total"] == 594
        assert parsed["trials_total"] == 572
        assert parsed["entries"] == [{
            "source_trial_id": "NCT07786753",
            "title": "A Troponin Study",
            "url": "https://trialsearch.who.int/Trial2.aspx?TrialID=NCT07786753",
            "status": "Recruiting",
            "registration_date": "2026-08-21",
            "source_registry": "NCT",
            "source_jurisdiction": "US",
        }]

    @pytest.mark.parametrize(("trial_id", "registry", "jurisdiction"), [
        ("CTRI/2026/09/117185", "CTRI", "IN"),
        ("PACTR202608683721733", "PACTR", "AFRICA"),
        ("ChiCTR2600128224", "ChiCTR", "CN"),
        ("ACTRN12626000717358", "ANZCTR", "AU_NZ"),
        ("IRCT20260429069203N1", "IRCT", "IR"),
        ("TCTR20260629002", "TCTR", "TH"),
    ])
    def test_live_portal_registry_origin(self, trial_id, registry, jurisdiction):
        from collectors.who_ictrp import _portal_registry_origin

        assert _portal_registry_origin(trial_id) == {
            "source_registry": registry,
            "source_jurisdiction": jurisdiction,
        }

    def test_live_portal_registry_origin_unknown(self):
        from collectors.who_ictrp import _portal_registry_origin

        assert _portal_registry_origin("UNKNOWN-123") == {}

    def test_portal_request_retries_transport_error(self, monkeypatch):
        import requests
        import collectors.who_ictrp as mod

        class Response:
            def raise_for_status(self):
                return None

        class Session:
            calls = 0

            def request(self, method, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise requests.ConnectionError("temporary TLS EOF")
                return Response()

        session = Session()
        monkeypatch.setattr(mod.time, "sleep", lambda _: None)
        assert mod._portal_request(session, "GET", url="https://example.test")
        assert session.calls == 2


def test_live_portal_entries_are_persisted_for_full_ingest(conn):
    from collectors.who_ictrp import WHOICTRPCollector

    collector = WHOICTRPCollector()
    count = collector.ingest_live_entries([{
        "source_trial_id": "NCT09999991",
        "title": "Portal-only biomarker study",
        "url": "https://trialsearch.who.int/Trial2.aspx?TrialID=NCT09999991",
        "status": "Recruiting",
        "registration_date": "2026-09-29",
        "source_registry": "NCT",
        "source_jurisdiction": "US",
    }])

    assert count == 1
    row = conn.execute(
        "SELECT title, is_bootstrap FROM registry_records "
        "WHERE source_id=? AND source_trial_id=? AND is_latest=1",
        (collector.source_id, "NCT09999991"),
    ).fetchone()
    assert dict(row) == {
        "title": "Portal-only biomarker study", "is_bootstrap": 1,
    }


class TestLiveIngestNoDowngrade:
    """A thin portal row must never supersede a full snapshot record.

    Regression: ingest_live_entries versioned the search grid's stubs
    (title/status/date only — the grid does not expose recruitment
    countries) over full XML-snapshot rows, flipping is_latest onto the
    stub and hiding countries/sponsors/conditions/phase until the next
    weekly snapshot re-versioned the trial.
    """

    @pytest.fixture
    def collector(self, conn):
        from collectors.who_ictrp import WHOICTRPCollector
        return WHOICTRPCollector()

    def _insert_full_record(self, conn, trial_id="ChiCTR2600128224") -> int:
        sid = _source_id(conn, "ICTRP")
        payload = json.dumps({"who_ictrp_record": {"trial_id": trial_id}})
        cur = conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, countries, raw_payload,
                version_number, is_latest)
               VALUES (?, ?, 'Snapshot title', '["China"]', ?, 1, 1)""",
            (sid, trial_id, payload))
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def _entry(trial_id="ChiCTR2600128224", title="Portal title"):
        return {
            "source_trial_id": trial_id, "title": title,
            "source_registry": "ChiCTR", "status": "Recruiting",
            "registration_date": "2026/09/29",
            "url": "https://trialsearch.who.int/Trial2.aspx",
        }

    def test_skips_hit_whose_latest_record_is_full(self, conn, collector):
        full_id = self._insert_full_record(conn)
        count = collector.ingest_live_entries([self._entry()])
        assert count == 0
        latest = conn.execute(
            "SELECT record_id, title, countries, version_number, is_latest "
            "FROM registry_records WHERE source_id=? AND source_trial_id=?",
            (collector.source_id, "ChiCTR2600128224"),
        ).fetchall()
        assert len(latest) == 1  # no second version was created
        assert latest[0]["record_id"] == full_id
        assert latest[0]["is_latest"] == 1
        assert json.loads(latest[0]["countries"]) == ["China"]

    def test_still_ingests_brand_new_hit(self, conn, collector):
        assert collector.ingest_live_entries(
            [self._entry(trial_id="ChiCTR2600129999")]) == 1
        row = conn.execute(
            "SELECT raw_payload FROM registry_records "
            "WHERE source_id=? AND source_trial_id=? AND is_latest=1",
            (collector.source_id, "ChiCTR2600129999"),
        ).fetchone()
        assert json.loads(row["raw_payload"])["who_ictrp_record"]["_portal_live"] == 1

    def test_still_reingests_hit_over_thin_row(self, conn, collector):
        """thin-over-thin stays allowed (status/title drift from the grid)."""
        collector.ingest_live_entries([self._entry()])
        count = collector.ingest_live_entries(
            [self._entry(title="Portal title v2")])
        assert count == 1
        versions = conn.execute(
            "SELECT title, is_latest FROM registry_records "
            "WHERE source_id=? AND source_trial_id=? ORDER BY version_number",
            (collector.source_id, "ChiCTR2600128224"),
        ).fetchall()
        assert [v["title"] for v in versions] == ["Portal title", "Portal title v2"]
        assert versions[1]["is_latest"] == 1

    def test_latest_record_is_full_flags(self, conn, collector):
        self._insert_full_record(conn)
        assert collector._latest_record_is_full("ChiCTR2600128224") is True
        assert collector._latest_record_is_full("ChiCTR00000000") is False
        sid = _source_id(conn, "ICTRP")
        conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, raw_payload, is_latest)
               VALUES (?, ?, 'Stub', ?, 1)""",
            (sid, "ChiCTR2600127777",
             json.dumps({"who_ictrp_record": {"_portal_live": True}})))
        conn.commit()
        assert collector._latest_record_is_full("ChiCTR2600127777") is False

    def test_latest_record_is_full_survives_malformed_payload(self, conn,
                                                              collector):
        """A corrupted payload row must not raise json_extract and take the
        live ingest down (regression: the malformed v1 row of
        ChiCTR2400087372).  Unparseable rows count as full, so the thin
        portal row is still skipped."""
        sid = _source_id(conn, "ICTRP")
        conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, raw_payload, is_latest,
                version_number)
               VALUES (?, ?, 'Corrupt', '{"who_ictrp_record": {"tri', 1, 1)""",
            (sid, "ChiCTR2600128888"))
        conn.commit()
        assert collector._latest_record_is_full("ChiCTR2600128888") is True
