"""Identifier-pattern regression tests (myocarditis case, 2026-09-08).

The CTR pattern used to be ``CTR\\d{8}`` with no left boundary, so
ChiCTR's own registration number ``ChiCTR2600131888`` had ``CTR26001318``
extracted from its 3rd character.  Every ChiCTR record in the same
100-number block shared that fake ID, and identifier auto-confirm merged
6 mega-masters of unrelated trials.  These tests pin the boundary fix
(lookbehind) and the year-prefix tightening (``CTR20\\d{6}``).
"""
from __future__ import annotations

from core.identifier_extraction import (
    IDENTIFIER_PATTERNS,
    get_identifier_type,
    _search_text,
)


def _extract(raw_text: str) -> set[tuple[str, str]]:
    results: list = []
    _search_text(raw_text, results, set())
    return {(r["identifier_type"], r["identifier_value"]) for r in results}


def test_real_ctr_number_still_matches():
    assert get_identifier_type("CTR20263443") == "CTR"
    assert ("CTR", "CTR20263443") in _extract("登记号：CTR20263443")


def test_abbreviated_year_ctr_number_rejected():
    # 缩写年伪号（26 = 年份缩写）不再命中 CTR 模式
    assert get_identifier_type("CTR26001318") == "ProtocolNumber"
    assert ("CTR", "CTR26001318") not in _extract("CTR26001318")


def test_chictr_number_no_longer_leaks_ctr_substring():
    got = _extract("注册号：ChiCTR2600131888&nbsp;")
    assert ("ChiCTR", "ChiCTR2600131888") in got
    assert all(v != "CTR26001318" for _, v in got)
    assert all(t != "CTR" for t, _ in got)


def test_nct_left_boundary_blocks_inner_match():
    assert ("NCT", "NCT04093297") in _extract("see (NCT04093297) for details")
    assert _extract("XXNCT04093297") == set()  # 前面贴着字母 → 拒绝


def test_other_patterns_keep_left_boundary_semantics():
    assert ("UTN", "U1111-1234-5678") in _extract("UTN: U1111-1234-5678")
    # 边界修复钉的是「类型判定」：贴着字母时不再判为 UTN
    # （松散协议号兜底仍可能按 ProtocolNumber 捕获，属既有设计，不在此断言）
    assert ("UTN", "U1111-1234-5678") not in _extract("XU1111-1234-5678")


def test_ctis_and_isrctn_identifiers_are_first_class():
    assert get_identifier_type("2026-525681-22-00") == "CTIS"
    assert get_identifier_type("isrctn71771495") == "ISRCTN"
    got = _extract("Cross-registered as 2026-525681-22-00 and ISRCTN71771495")
    assert ("CTIS", "2026-525681-22-00") in got
    assert ("ISRCTN", "ISRCTN71771495") in got


def test_eudract_identifier_does_not_match_ctis_prefix():
    assert get_identifier_type("2014-001354-42") == "EudraCT"
    assert ("EudraCT", "2014-001354-42") in _extract("EudraCT 2014-001354-42")
    got = _extract("CTIS 2026-525681-22-00")
    assert ("CTIS", "2026-525681-22-00") in got
    assert all(kind != "EudraCT" for kind, _ in got)


def test_patterns_dict_documents_boundary_policy():
    # 守住「带边界的模式」不被无意回退
    for name, pattern in IDENTIFIER_PATTERNS.items():
        assert "(?<![A-Za-z])" in pattern.pattern, f"{name} lost left boundary"
    assert IDENTIFIER_PATTERNS["CTR"].pattern.endswith(r"CTR20\d{6})") or \
        "CTR20" in IDENTIFIER_PATTERNS["CTR"].pattern
