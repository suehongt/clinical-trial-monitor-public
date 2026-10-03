"""Computed field-coverage overview: SQL math, empty sentinels, panel render.

Covers core.quality_checks.field_coverage_by_source() (single GROUP BY query
over registry_records, is_latest = 1) and the computed 字段完整度矩阵 in
ct_report.render._render_quality_overview() — including the fallback to the
static FIELD_QUALITY_MATRIX when the DB is unusable.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _add_record(db_path: Path, short_name: str, trial_id: str,
                version_number: int = 1, is_latest: int = 1, **fields) -> None:
    """Insert one registry_record for the given source into the temp DB."""
    conn = sqlite3.connect(str(db_path))
    source_id = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()[0]
    cols = ["source_id", "source_trial_id", "data_hash",
            "version_number", "is_latest"]
    vals = [source_id, trial_id, "h", version_number, is_latest]
    for key, val in fields.items():
        cols.append(key)
        vals.append(val)
    conn.execute(
        f"INSERT INTO registry_records ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(vals))})",
        vals,
    )
    conn.commit()
    conn.close()


def _seed_coverage_db(db_path: Path) -> None:
    """3 NCT + 2 CTR latest records with mixed field availability.

    NCT: two records with real primary/secondary endpoints, one with
    secondary_endpoints='[]' and NULL primary_endpoint.
    CTR: one record with a locations JSON array, one with NULL.
    """
    _add_record(db_path, "NCT", "NCT00000001",
                primary_endpoint="progression-free survival",
                secondary_endpoints='["overall survival"]',
                eligibility_criteria="Inclusion: adults 18-75",
                study_phase="Phase 3")
    _add_record(db_path, "NCT", "NCT00000002",
                primary_endpoint="overall survival",
                secondary_endpoints='["PFS", "ORR"]',
                eligibility_criteria="Inclusion: histologically confirmed",
                study_phase="Phase 2")
    _add_record(db_path, "NCT", "NCT00000003",
                secondary_endpoints="[]",           # empty JSON array → N/A
                study_phase="Phase 1")
    _add_record(db_path, "CTR", "CTR20250001",
                locations='[{"site": "北京协和医院"}]')
    _add_record(db_path, "CTR", "CTR20250002")


def test_coverage_math(test_db):
    """Exact (covered, total) tuples per source/field over is_latest=1 rows."""
    from core.quality_checks import field_coverage_by_source

    _seed_coverage_db(test_db)
    cov = field_coverage_by_source()
    assert cov["NCT"]["Secondary Endpoints"] == (2, 3)   # '[]' not covered
    assert cov["NCT"]["Primary Endpoint"] == (2, 3)      # one NULL
    assert cov["CTR"]["Locations"] == (1, 2)             # one NULL
    # Sources with no latest rows are simply absent
    assert "ICTRP" not in cov
    assert "ChiCTR" not in cov


def test_dash_and_empty_array_are_missing(test_db):
    """'-'/whitespace-only sentinels count as NOT covered; old versions don't count."""
    from core.quality_checks import field_coverage_by_source

    _add_record(test_db, "NCT", "NCT00000010",
                eligibility_criteria="-", primary_endpoint="PFS")
    _add_record(test_db, "NCT", "NCT00000011", eligibility_criteria="   ")
    _add_record(test_db, "NCT", "NCT00000012",
                eligibility_criteria="Inclusion: adults")
    # Superseded version of NCT00000012: must not change the count
    _add_record(test_db, "NCT", "NCT00000012", version_number=0, is_latest=0,
                eligibility_criteria="old superseded criteria")

    cov = field_coverage_by_source()
    assert cov["NCT"]["Eligibility"] == (1, 3)   # '-' and '   ' are missing
    assert cov["NCT"]["Primary Endpoint"] == (1, 3)


def test_render_panel_shows_computed(test_db):
    """The panel renders computed covered/total cells with the tier emojis."""
    from ct_report.render import _coverage_cell, _render_quality_overview

    # Tier boundaries: ✅ ≥95%, ⚠️ ≥50%, ❌ >0%, ➖ 0 covered
    assert _coverage_cell(95, 100) == "✅ 95/100 (95.0%)"
    assert _coverage_cell(50, 100) == "⚠️ 50/100 (50.0%)"
    assert _coverage_cell(1, 3) == "❌ 1/3 (33.3%)"
    assert _coverage_cell(0, 4) == "➖ 0/4 (0.0%)"
    assert _coverage_cell(0, 0) == "➖ 0/0 (0.0%)"

    _seed_coverage_db(test_db)
    by_source = {"NCT": [1, 2, 3], "CTR": [1, 2],
                 "ChiCTR": [], "ICTRP_native": [], "ICTRP_ChiCTR": []}
    html = _render_quality_overview(by_source)

    # NCT primary endpoint 2/3 → ⚠️ tier; study phase 3/3 → ✅ tier;
    # CTR sponsors unset → ➖ tier
    assert "⚠️ 2/3 (66.7%)" in html
    assert "✅ 3/3 (100.0%)" in html
    assert "➖ 0/2 (0.0%)" in html
    # Hardcoded claims from the static matrix must be gone
    assert "⚠️ 76%" not in html
    assert "✅ 92%" not in html


def test_fallback_to_matrix(test_db, monkeypatch):
    """When coverage can't be computed, the old FIELD_QUALITY_MATRIX renders."""
    import core.quality_checks as qchecks
    from ct_report.render import _render_quality_overview

    monkeypatch.setattr(qchecks, "field_coverage_by_source", lambda: {})
    by_source = {"NCT": [1], "CTR": [1],
                 "ChiCTR": [], "ICTRP_native": [], "ICTRP_ChiCTR": []}
    html = _render_quality_overview(by_source)

    # Markers from the hand-written matrix (ICTRP Secondary Endpoints / Arm Group)
    assert "⚠️ 76%" in html
    assert "✅ 92%" in html
    # Computed cells must be absent
    assert "2/3 (66.7%)" not in html
