"""Cross-source list-field format unification (JSON string arrays).

Guards the canonical storage format for conditions / interventions /
sponsors / locations / secondary_endpoints introduced in the format
unification: every collector must emit JSON arrays, and the one-off
migration (scripts/migrate_json_fields.py) must be lossless and idempotent.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def chictr_record():
    from collectors.chictr import ChiCTRCollector

    parsed = {
        "source_trial_id": "ChiCTR2600128415",
        "title": "急性心肌梗死研究",
        "conditions": "心肌梗死",
        "interventions": "新型药物A",
        "sponsor": "某三甲医院",
        "secondary_endpoints": "主要不良心血管事件发生率",
        "registration_date": "2026-01-15",
    }
    raw = {
        "chictr_no": "ChiCTR2600128415",
        "proj_id": "291119",
        "html": "<html/>",
        "parsed_fields": parsed,
    }
    return ChiCTRCollector().normalise(raw)


@pytest.fixture
def ctr_record():
    from collectors.chinadrugtrials import ChinaDrugTrialsCollector

    parsed = {
        "source_trial_id": "CTR20250001",
        "title": "试验通俗题目",
        "conditions": "急性心肌梗死",
        "drug_name": "阿司匹林片",
        "sponsors": "申办方甲\n申办方乙",  # multi-line → split into items
        "locations": "北京\n上海",
        "secondary_endpoints": "次要终点1",
    }
    raw = {"ctr_number": "CTR20250001", "uuid": "abc", "html": "<html/>",
           "parsed_fields": parsed}
    return ChinaDrugTrialsCollector().normalise(raw)


@pytest.fixture
def ictrp_record():
    from collectors.who_ictrp import WHOICTRPCollector

    raw = {
        "reg_name": "ClinicalTrials.gov",
        "trial_id": "NCT99999999",
        "public_title": "A MI Trial",
        "hc_freetext": "Myocardial Infarction",
        "i_freetext": "Aspirin",
        "secondary_outcome": ["MACE at 12 months", "All-cause mortality"],
        "primary_sponsor": "Uni X",
    }
    return WHOICTRPCollector().normalise(raw)


class TestCollectorJsonArrays:
    """All list-type fields must be JSON string arrays."""

    def test_chictr(self, chictr_record):
        for field in ("conditions", "interventions", "sponsors",
                      "secondary_endpoints"):
            val = getattr(chictr_record, field)
            assert val is None or isinstance(json.loads(val), list), field

    def test_chictr_countries(self, chictr_record):
        assert json.loads(chictr_record.countries) == ["中国"]

    def test_chictr_secondary_endpoints_content(self, chictr_record):
        assert json.loads(chictr_record.secondary_endpoints) == \
            ["主要不良心血管事件发生率"]

    def test_chinadrugtrials(self, ctr_record):
        for field in ("conditions", "interventions", "sponsors", "locations",
                      "secondary_endpoints"):
            val = getattr(ctr_record, field)
            assert val is None or isinstance(json.loads(val), list), field

    def test_chinadrugtrials_multiline_split(self, ctr_record):
        assert json.loads(ctr_record.sponsors) == ["申办方甲", "申办方乙"]
        assert json.loads(ctr_record.locations) == ["北京", "上海"]

    def test_chinadrugtrials_drug_name_as_interventions(self, ctr_record):
        assert json.loads(ctr_record.interventions) == ["阿司匹林片"]

    def test_who_ictrp(self, ictrp_record):
        assert json.loads(ictrp_record.conditions) == ["Myocardial Infarction"]
        assert json.loads(ictrp_record.interventions) == ["Aspirin"]
        assert json.loads(ictrp_record.secondary_endpoints) == \
            ["MACE at 12 months", "All-cause mortality"]


class TestChangeDetectionCompat:
    """compare_values must not emit false changes between equivalent
    array spellings; legacy text vs array IS a change, which is exactly
    why migrate_json_fields.py must run once."""

    def test_equivalent_arrays_order_insensitive(self):
        from core.change_detection import compare_values
        assert compare_values('["b","a"]', '["a","b"]', "conditions") is False

    def test_legacy_text_vs_array_is_a_change(self):
        from core.change_detection import compare_values
        assert compare_values("心肌梗死", '["心肌梗死"]', "conditions") is True


class TestMigrateScript:
    """scripts/migrate_json_fields.migrate() must be lossless + idempotent."""

    def _insert_record(self, conn, source_id, trial_id, **fields):
        cols = {"source_id": source_id, "source_trial_id": trial_id,
                "version_number": 1, "is_latest": 1}
        cols.update(fields)
        conn.execute(
            f"INSERT INTO registry_records ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )

    def test_migrate_lossless_and_idempotent(self, test_db):
        from db.connection import get_connection
        from db.schema import create_schema
        from scripts.migrate_json_fields import migrate

        create_schema()
        conn = get_connection()
        source_id = conn.execute(
            "SELECT source_id FROM registry_sources WHERE short_name='NCT'"
        ).fetchone()[0]

        self._insert_record(conn, source_id, "NCT00000001",
                            conditions="心肌梗死",               # plain text
                            interventions="A\nB",                # newline list
                            sponsors='["已经", "是数组"]',         # canonical
                            secondary_endpoints=None)            # NULL
        conn.commit()

        stats = migrate(conn, apply=True)
        assert stats["conditions"] == 1
        assert stats["interventions"] == 1
        assert stats["sponsors"] == 0

        row = conn.execute(
            "SELECT conditions, interventions, sponsors FROM registry_records "
            "WHERE source_trial_id='NCT00000001'"
        ).fetchone()
        assert json.loads(row["conditions"]) == ["心肌梗死"]
        assert json.loads(row["interventions"]) == ["A", "B"]
        assert json.loads(row["sponsors"]) == ["已经", "是数组"]

        # Idempotent: second pass finds nothing to do
        stats2 = migrate(conn, apply=True)
        assert all(v == 0 for v in stats2.values())
