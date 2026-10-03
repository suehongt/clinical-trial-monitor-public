"""End-to-end report generation smoke test on a throwaway database.

Covers the full generate_report() path (query → diff checkpoint → render)
that previously had zero coverage — the 2026-09 "raw [] in Secondary
Endpoints" bug lived here and no test would have caught it.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def report_db(test_db, monkeypatch):
    """Route ct_report's own connection (ct_report.paths.DB_PATH) to the temp DB."""
    import ct_report.query

    monkeypatch.setattr(ct_report.query, "DB_PATH", test_db)
    return test_db


def _seed_trial(db_path: Path, trial_id: str = "NCT07486791") -> None:
    conn = sqlite3.connect(str(db_path))
    source_id = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = 'NCT'"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, study_type_id,
            enrollment, secondary_endpoints, conditions, sponsors,
            eligibility_criteria, primary_endpoint, raw_payload,
            data_hash, version_number, is_latest)
           VALUES (?, ?, ?,
                   (SELECT status_type_id FROM status_types WHERE label = 'Recruiting'),
                   (SELECT study_type_id FROM study_types WHERE label = 'Interventional'),
                   ?, ?, ?, ?, ?, ?, ?, 'x', 1, 1)""",
        (
            source_id,
            trial_id,
            '<B>ACS & "quoted"</B> myocardial infarction Baduanjin study',
            65,
            "[]",                                        # empty JSON array endpoint
            '["6MWT < 300 m & rising"]',                 # markup inside the array
            '[{"name": "Qi & Co", "role": "lead"}]',
            "1. Age \\<= 85 years\n2. Signed & dated consent",
            "6-minute walk test distance",
            '{"protocolSection": {"identificationModule": {"nctId": "%s"}}}' % trial_id,
        ),
    )
    conn.commit()
    conn.close()


def test_generate_report_escapes_and_renders(report_db, tmp_path):
    from ct_report.report import generate_report

    _seed_trial(report_db)
    path = generate_report(output=str(tmp_path / "report.html"),
                           force=True, english=True)

    html = Path(path).read_text(encoding="utf-8")
    # Hostile title must be escaped, never raw
    assert "&lt;B&gt;ACS &amp; &quot;quoted&quot;&lt;/B&gt;" in html
    assert "<B>ACS" not in html
    # Empty JSON-array endpoint renders as N/A, not raw "[]"
    assert ">[]<" not in html
    assert '<span class="na">-</span>' in html
    # Items inside JSON arrays, criteria lines and sponsors escape too
    assert "6MWT &lt; 300 m &amp; rising" in html
    assert "Age &lt;= 85 years" in html
    assert "Qi &amp; Co" in html


def test_generate_report_cross_source_siblings(report_db, tmp_path):
    """Two records on one master trial must cross-reference each other."""
    import sqlite3 as s3
    import uuid

    from ct_report.query import fetch_cross_source_siblings
    from ct_report.report import generate_report

    _seed_trial(report_db, "NCT00000001")

    # Second record (ICTRP mirror) on the same master trial
    conn = s3.connect(str(report_db))
    src_ictrp = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = 'ICTRP'"
    ).fetchone()[0]
    rec1 = conn.execute(
        "SELECT record_id FROM registry_records WHERE source_trial_id = 'NCT00000001'"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, data_hash, version_number, is_latest)
           VALUES (?, ?, 'mirror record', 'h2', 1, 1)""",
        (src_ictrp, "NCT00000001"),
    )
    rec2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    master_id = str(uuid.uuid4())
    conn.execute("INSERT INTO master_trials (master_trial_id, preferred_title) VALUES (?, ?)",
                 (master_id, "mirrored trial"))
    conn.execute(
        "INSERT INTO record_master_map (record_id, master_trial_id, match_method) VALUES (?, ?, ?)",
        (rec1, master_id, "identifier"))
    conn.execute(
        "INSERT INTO record_master_map (record_id, master_trial_id, match_method) VALUES (?, ?, ?)",
        (rec2, master_id, "identifier"))
    conn.commit()
    conn.close()

    siblings = fetch_cross_source_siblings()
    assert set(siblings.keys()) == {"NCT:NCT00000001", "ICTRP:NCT00000001"}
    assert siblings["NCT:NCT00000001"][0]["short_name"] == "ICTRP"

    path = generate_report(output=str(tmp_path / "r.html"), force=True, english=True)
    html = Path(path).read_text(encoding="utf-8")
    assert "Also registered on:" in html
    assert 'class="xsrc-chip"' in html


def test_generate_report_baselines_and_incremental(report_db, tmp_path):
    from ct_report.diffing import get_last_report
    from ct_report.report import generate_report

    _seed_trial(report_db)
    out = tmp_path / "report.html"
    generate_report(output=str(out), force=True, english=True)

    mi = get_last_report("mi_cross_source")
    assert mi is not None and mi["record_count"] == 1

    # A custom keyword set gets its own derived baseline and must not
    # overwrite the MI checkpoint (report_key isolation).
    assert Path(generate_report(output=str(tmp_path / "custom.html"),
                                force=True, english=True,
                                keywords=["baduanjin"])).exists()
    assert get_last_report("mi_cross_source") == mi
    conn = sqlite3.connect(str(report_db))
    keys = {r[0] for r in conn.execute("SELECT report_key FROM report_registry")}
    conn.close()
    assert any(k.startswith("cross_source_") for k in keys)

    # Second run on unchanged data → incremental empty-delta report
    html = Path(generate_report(output=str(out), english=True)).read_text(encoding="utf-8")
    assert "Incremental" in html


def test_no_checkpoint_leaves_baseline_untouched(report_db, tmp_path):
    """update_checkpoint=False must render the report but never move the baseline."""
    from ct_report.diffing import get_last_report
    from ct_report.report import generate_report

    _seed_trial(report_db)

    # One-off run: HTML is written, but no baseline row is created.
    out1 = tmp_path / "oneoff.html"
    path = generate_report(output=str(out1), force=True, english=True,
                           update_checkpoint=False)
    assert Path(path).exists()
    assert get_last_report("mi_cross_source") is None

    # Checkpointed run (default): baseline row now exists — capture its values.
    out2 = tmp_path / "baseline.html"
    generate_report(output=str(out2), force=True, english=True)
    row = get_last_report("mi_cross_source")
    assert row is not None and row["record_count"] == 1
    baseline = (row["report_file"], row["generated_at"], row["record_count"])

    # A later one-off run with a DIFFERENT output path must not move the baseline.
    time.sleep(1.1)  # generated_at has 1s resolution — make an overwrite observable
    out3 = tmp_path / "another_oneoff.html"
    path = generate_report(output=str(out3), force=True, english=True,
                           update_checkpoint=False)
    assert Path(path).exists()
    row = get_last_report("mi_cross_source")
    assert (row["report_file"], row["generated_at"], row["record_count"]) == baseline
    assert row["report_file"] == str(out2)


def test_pointer_change_logs_warning(report_db, caplog):
    """Re-pointing a registry key's report_file must emit a warning with both paths."""
    from ct_report.diffing import save_report_registry

    caplog.set_level(logging.DEBUG, logger="ct_report.diffing")
    caplog.clear()

    # First save: no existing row → no pointer-change warning
    save_report_registry("pointer_probe", "/tmp/r1.html", ["t1"], {})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    caplog.clear()
    # Different report_file for the same key → warning naming old and new paths
    save_report_registry("pointer_probe", "/tmp/r2.html", ["t1"], {})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "pointer_probe" in msg
    assert "/tmp/r1.html" in msg
    assert "/tmp/r2.html" in msg

    caplog.clear()
    # Same-file re-save → still no pointer-change warning
    save_report_registry("pointer_probe", "/tmp/r2.html", ["t1"], {})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_cli_no_checkpoint_flag():
    """`python -m ct_report.cli --help` must expose the --no-checkpoint flag."""
    r = subprocess.run(
        [sys.executable, "-m", "ct_report.cli", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "--no-checkpoint" in r.stdout
    assert "Traceback" not in r.stderr
