"""Report baseline-diff annotation tests — NEW/CHANGED chips + old→new blocks.

Covers the force-branch baseline diff (full rebuild still annotates the
delta without filtering the list), the incremental annotation, and the
card-level change-block rendering/escaping contract.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def report_db(test_db, monkeypatch):
    """Route ct_report's own connection (ct_report.paths.DB_PATH) to the temp DB."""
    import ct_report.query

    monkeypatch.setattr(ct_report.query, "DB_PATH", test_db)
    return test_db


def _seed_trial(db_path: Path, trial_id: str, *, enrollment: int = 120,
                title: str = "Myocardial infarction Baduanjin study") -> int:
    """Insert an is_latest v1 row; returns the record_id."""
    conn = sqlite3.connect(str(db_path))
    source_id = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = 'NCT'"
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, study_type_id,
            enrollment, conditions, sponsors, raw_payload,
            data_hash, version_number, is_latest)
           VALUES (?, ?, ?,
                   (SELECT status_type_id FROM status_types WHERE label = 'Recruiting'),
                   (SELECT study_type_id FROM study_types WHERE label = 'Interventional'),
                   ?, ?, ?, 'x', ?, 1, 1)""",
        (
            source_id,
            trial_id,
            title,
            enrollment,
            '["myocardial infarction"]',
            '[{"name": "Qi & Co", "role": "lead"}]',
            '{"protocolSection": {"identificationModule": {"nctId": "%s"}}}' % trial_id,
        ),
    )
    conn.commit()
    record_id = cur.lastrowid
    conn.close()
    return record_id


def _bump_enrollment(db_path: Path, trial_id: str, new_enrollment: int,
                     old_enrollment: int) -> None:
    """Simulate a source-side update: version the row + emit the event."""
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT record_id, version_number FROM registry_records "
        "WHERE source_trial_id = ? AND is_latest = 1", (trial_id,)).fetchone()
    old_id, version = row
    conn.execute("UPDATE registry_records SET is_latest = 0 "
                 "WHERE record_id = ?", (old_id,))
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, study_type_id,
            enrollment, conditions, sponsors, raw_payload,
            data_hash, version_number, is_latest)
           SELECT source_id, source_trial_id, title, status_id, study_type_id,
                  ?, conditions, sponsors, raw_payload,
                  'hash-v' || (? + 1), ? + 1, 1
           FROM registry_records WHERE record_id = ?""",
        (new_enrollment, version, version, old_id))
    new_id = cur.lastrowid
    conn.execute("UPDATE registry_records SET superseded_by = ? "
                 "WHERE record_id = ?", (new_id, old_id))
    conn.execute(
        """INSERT INTO trial_events (record_id, event_type, field_name,
           old_value, new_value, change_category, event_hash)
           VALUES (?, 'field_change', 'enrollment', ?, ?, 'enrollment', ?)""",
        (new_id, str(old_enrollment), str(new_enrollment),
         f"hash-{trial_id}-{new_enrollment}"))
    conn.commit()
    conn.close()


class TestForceReportAnnotation:
    def test_changed_trial_shows_chip_and_old_new_block(self, report_db, tmp_path):
        from ct_report.report import generate_report

        _seed_trial(report_db, "NCT00000001", enrollment=120)
        generate_report(output=str(tmp_path / "r1.html"), force=True,
                        english=False)

        _bump_enrollment(report_db, "NCT00000001", 240, 120)
        html = Path(generate_report(
            output=str(tmp_path / "r2.html"), force=True,
            english=False)).read_text(encoding="utf-8")

        # 全量报告仍是全集，但变更卡片带 chip + old→new 块
        assert 'chip chip-changed' in html
        assert "变更" in html
        assert "change-block" in html
        assert "入组人数" in html
        assert "120" in html and "240" in html
        assert "较上次报告" in html and "变更 1 项" in html
        # 全集未筛：报告头仍是「总试验数」语义
        assert "跨源综合报告" in html

    def test_new_trial_gets_new_chip_others_untouched(self, report_db, tmp_path):
        from ct_report.report import generate_report

        _seed_trial(report_db, "NCT00000001")
        generate_report(output=str(tmp_path / "r1.html"), force=True,
                        english=True)
        _seed_trial(report_db, "NCT00000002",
                    title="Brand new myocardial infarction trial")

        html = Path(generate_report(
            output=str(tmp_path / "r2.html"), force=True,
            english=True)).read_text(encoding="utf-8")

        assert html.count('chip chip-new') == 1
        assert "NEW" in html
        assert "Since previous report" in html
        assert "1 new" in html
        # 未变化的旧试验不打标
        assert html.count('chip chip-changed') == 0

    def test_first_force_report_without_baseline_has_no_chips(
            self, report_db, tmp_path):
        from ct_report.report import generate_report

        _seed_trial(report_db, "NCT00000001")
        html = Path(generate_report(
            output=str(tmp_path / "r.html"), force=True,
            english=False)).read_text(encoding="utf-8")
        assert 'chip chip-new' not in html
        assert 'chip chip-changed' not in html
        assert "较上次报告" not in html

    def test_incremental_report_keeps_annotations(self, report_db, tmp_path):
        from ct_report.report import generate_report

        _seed_trial(report_db, "NCT00000001", enrollment=120)
        generate_report(output=str(tmp_path / "r1.html"), force=True,
                        english=False)
        _bump_enrollment(report_db, "NCT00000001", 240, 120)

        html = Path(generate_report(
            output=str(tmp_path / "r2.html"),  # 增量（无 force）
            english=False)).read_text(encoding="utf-8")
        assert 'chip chip-changed' in html
        assert "change-block" in html
        assert "入组人数" in html


class TestChangeBlockEscaping:
    def test_event_values_are_escaped(self):
        from ct_report.render import _change_block_html

        html = _change_block_html([
            {"field_name": "sponsors", "old_value": '["<b>Pfizer</b>"]',
             "new_value": '["Roche & Co"]'},
        ], english=False)
        assert "<b>Pfizer</b>" not in html
        assert "&lt;b&gt;Pfizer&lt;/b&gt;" in html
        assert "Roche &amp; Co" in html

    def test_long_values_truncated(self):
        from ct_report.render import _change_block_html

        html = _change_block_html([
            {"field_name": "primary_endpoint",
             "old_value": "x" * 200, "new_value": "y" * 200},
        ], english=False)
        assert "x" * 200 not in html
        assert "…" in html

    def test_unknown_field_falls_back_to_raw_name(self):
        from ct_report.render import _change_block_html

        html = _change_block_html([
            {"field_name": "some_new_field", "old_value": "a",
             "new_value": "b"},
        ], english=True)
        assert "some_new_field" in html

    def test_empty_events_render_nothing(self):
        from ct_report.render import _change_block_html

        assert _change_block_html([], english=False) == ""
        assert _change_block_html(None, english=False) == ""
