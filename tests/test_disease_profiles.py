"""Multi-disease profile support: keyword-set-scoped report baselines.

Guards the baseline-key isolation added for CT_DISEASE_PROFILE: each
keyword set must get its own report_registry key so that running a
multiple-myeloma report can never overwrite the myocardial-infarction
incremental baseline (and vice versa).
"""
from __future__ import annotations


MI_SET = ["心肌梗死", "myocardial infarction", "acute coronary syndrome",
          "心梗", "冠脉综合征", "AMI", "STEMI", "NSTEMI"]
MM_SET = ["多发性骨髓瘤", "骨髓瘤", "浆细胞骨髓瘤", "浆细胞白血病",
          "multiple myeloma", "plasma cell myeloma", "plasma cell leukemia",
          "myeloma", "MGUS"]


def _key_for(monkeypatch, keywords, search_keywords):
    import ct_report.report as report_mod
    monkeypatch.setattr(report_mod, "SEARCH_KEYWORDS", list(search_keywords))
    return report_mod._report_key(keywords)


def test_default_mi_profile_keeps_historical_key(monkeypatch):
    """MI profile (the default): keywords=None -> historical baseline key."""
    assert _key_for(monkeypatch, None, MI_SET) == "mi_cross_source"
    assert _key_for(monkeypatch, MI_SET, MI_SET) == "mi_cross_source"


def test_mm_profile_never_writes_mi_baseline(monkeypatch):
    """Regression: under the MM profile, keywords=None used to compare the
    MM default set against itself and return 'mi_cross_source', clobbering
    the MI baseline.  It must get a derived key instead."""
    key = _key_for(monkeypatch, None, MM_SET)
    assert key != "mi_cross_source"
    assert key.startswith("cross_source_")


def test_explicit_keyword_sets_are_stable_and_distinct(monkeypatch):
    k1 = _key_for(monkeypatch, MM_SET, MI_SET)
    k2 = _key_for(monkeypatch, MM_SET, MI_SET)
    k3 = _key_for(monkeypatch, ["breast cancer", "乳腺癌"], MI_SET)
    assert k1 == k2                      # stable per keyword set
    assert k1 != k3                      # different sets -> different keys
    assert k1 != "mi_cross_source"


def test_keyword_order_and_case_do_not_change_key(monkeypatch):
    a = _key_for(monkeypatch, ["Multiple Myeloma", "myeloma"], MI_SET)
    b = _key_for(monkeypatch, ["myeloma", "Multiple Myeloma"], MI_SET)
    assert a == b
