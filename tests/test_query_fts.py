"""FTS-prefilter tests for ct_report.query.query_trials.

Pins the trigram-prefilter contract:
- the FTS path and the legacy all-LIKE path return identical trial-id sets;
- auto-detection (use_fts=None) uses FTS when the index is populated and
  falls back to LIKE when the index is stale or the table is missing;
- keywords shorter than 3 characters (e.g. the 2-char CJK 心梗) never go
  through FTS — the trigram tokenizer silently matches nothing below 3 chars;
- scientific_title-only matches are still found (records_fts does not index
  that column);
- keywords containing double quotes are escaped, not broken.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

import ct_report.query

# Mixed set: two long ASCII keywords, one 2-char CJK keyword (LIKE-only) and
# one keyword with an embedded double quote (FTS5 phrase escaping).
KEYWORDS = ["myocardial infarction", "HFpEF", "心梗", 'pain"ful']

EXPECTED_IDS = {
    "NCT00000001",  # title-only match
    "NCT00000002",  # conditions-only match
    "NCT00000003",  # scientific_title-only match (FTS blind spot)
    "NCT00000004",  # 2-char CJK keyword in conditions
    "NCT00000006",  # embedded double quote in title
}


@pytest.fixture
def query_db(test_db, monkeypatch):
    """Route ct_report.query's connection (ct_report.query.DB_PATH) to the
    temp DB.  create_schema() (via the test_db fixture) installs the
    records_fts AFTER INSERT trigger, so seeded rows auto-populate the index."""
    monkeypatch.setattr(ct_report.query, "DB_PATH", test_db)
    return test_db


def _seed(db_path: Path) -> None:
    """Seed NCT records covering every match surface + one non-matching row."""
    rows = [
        # (trial_id, title, scientific_title, conditions)
        ("NCT00000001",
         "Novel Baduanjin Programme After Acute Myocardial Infarction",
         "A Randomised Baduanjin Exercise Study",
         "Type 2 Diabetes"),
        ("NCT00000002",
         "Wearable Monitoring Study",
         "Digital Health Analytics Trial",
         '["HFpEF Cohort", "Hypertension"]'),
        ("NCT00000003",
         "Telephone-Based Care Management Trial",
         "Remote Monitoring After Myocardial Infarction in Older Adults",
         "Hypertension; Atrial Fibrillation"),
        ("NCT00000004",
         "胸痛中心注册研究",
         "Chest Pain Registry China",
         "冠心病, 心梗, 糖尿病"),
        ("NCT00000005",
         "Seasonal Influenza Vaccination Study",
         "Vaccine Effectiveness In Primary Care",
         "Influenza"),
        ("NCT00000006",
         'Study of Pain"ful Myocardial Infarction Syndrome',
         "Analgesia Outcomes Trial",
         "Post-cardiac injury"),
    ]
    conn = sqlite3.connect(str(db_path))
    nct = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = 'NCT'"
    ).fetchone()[0]
    for tid, title, sci, cond in rows:
        conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, scientific_title, conditions,
                version_number, is_latest)
               VALUES (?, ?, ?, ?, ?, 1, 1)""",
            (nct, tid, title, sci, cond),
        )
    conn.commit()
    conn.close()


def _ids(trials):
    return {t["source_trial_id"] for t in trials}


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_fts_path_equals_like_path(query_db, caplog):
    """Forced FTS path and forced legacy path return identical id sets —
    and the FTS run must really take the FTS path (not silently fall back)."""
    _seed(query_db)
    caplog.set_level(logging.INFO, logger="ct_report.query")
    caplog.clear()
    fts = ct_report.query.query_trials(keywords=KEYWORDS, use_fts=True)
    assert any("Query prefilter: FTS (trigram)" in m for m in _messages(caplog))
    assert not any("retrying once with legacy LIKE" in m for m in _messages(caplog))

    caplog.clear()
    like = ct_report.query.query_trials(keywords=KEYWORDS, use_fts=False)
    assert any("Query prefilter: LIKE fallback" in m for m in _messages(caplog))

    assert _ids(fts) == _ids(like) == EXPECTED_IDS


def test_auto_uses_fts_when_ready(query_db, caplog):
    """use_fts=None on a seeded, populated DB logs the FTS path and returns
    the expected rows."""
    _seed(query_db)
    caplog.set_level(logging.INFO, logger="ct_report.query")
    caplog.clear()

    trials = ct_report.query.query_trials(keywords=KEYWORDS)  # use_fts=None

    assert _ids(trials) == EXPECTED_IDS
    assert any("Query prefilter: FTS (trigram)" in m for m in _messages(caplog))


def test_stale_index_falls_back_to_like(query_db, caplog):
    """An emptied (stale) index must be detected and route to the legacy LIKE
    path, still returning the expected rows (they match via LIKE)."""
    _seed(query_db)
    conn = sqlite3.connect(str(query_db))
    conn.execute("DELETE FROM records_fts")
    conn.commit()
    conn.close()

    caplog.set_level(logging.INFO, logger="ct_report.query")
    caplog.clear()

    trials = ct_report.query.query_trials(keywords=KEYWORDS)  # use_fts=None

    assert _ids(trials) == EXPECTED_IDS
    msgs = _messages(caplog)
    assert any("Query prefilter: LIKE fallback" in m for m in msgs)
    assert not any("Query prefilter: FTS" in m for m in msgs)


def test_missing_table_falls_back(query_db, caplog):
    """A database without records_fts must not raise and must return the
    expected rows via the legacy path."""
    _seed(query_db)
    conn = sqlite3.connect(str(query_db))
    conn.execute("DROP TABLE records_fts")
    conn.commit()
    conn.close()

    caplog.set_level(logging.INFO, logger="ct_report.query")
    caplog.clear()

    trials = ct_report.query.query_trials(keywords=KEYWORDS)  # use_fts=None

    assert _ids(trials) == EXPECTED_IDS
    assert any("Query prefilter: LIKE fallback" in m for m in _messages(caplog))


def test_short_cjk_keyword_matches(query_db):
    """A 2-char CJK keyword (心梗) must match via the <3-char LIKE guard —
    sending it through the trigram index would silently return nothing."""
    _seed(query_db)

    trials = ct_report.query.query_trials(keywords=["心梗"])

    assert _ids(trials) == {"NCT00000004"}
    assert "心梗" in (trials[0]["conditions"] or "")


def test_forced_fts_with_missing_table_retries_like(query_db, caplog):
    """Belt-and-braces: a forced FTS query over a missing table warns and
    retries once with the legacy WHERE instead of crashing."""
    _seed(query_db)
    conn = sqlite3.connect(str(query_db))
    conn.execute("DROP TABLE records_fts")
    conn.commit()
    conn.close()

    caplog.set_level(logging.INFO, logger="ct_report.query")
    caplog.clear()

    trials = ct_report.query.query_trials(keywords=KEYWORDS, use_fts=True)

    assert _ids(trials) == EXPECTED_IDS
    assert any("retrying once with legacy LIKE WHERE" in m for m in _messages(caplog))
