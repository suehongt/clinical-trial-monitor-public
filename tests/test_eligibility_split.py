"""Regression tests for the eligibility inclusion/exclusion split.

ClinicalTrials.gov exports the section labels as bare lines with no colon
('* INCLUSION CRITERIA' / 'EXCLUSION CRITERIA'), which the old colon-only
splitter silently failed on — the whole text stayed in inclusion and
exclusion rendered as '-'.  These tests pin every label form the
registries emit.
"""
from __future__ import annotations

from ct_report.render import _eligibility_split


def test_bare_label_lines_without_colon():
    # NCT07806149 shape: bullet-prefixed inclusion label, bare exclusion label
    text = ("* INCLUSION CRITERIA\n"
            "* Adults age \\>= 21 years\n"
            "* Pregnant women are excluded\n"
            "\n"
            "EXCLUSION CRITERIA\n"
            "\n"
            "* Does not consent to participate\n"
            "* Pregnant\n")
    inc, exc = _eligibility_split(text)
    assert inc == "* Adults age \\>= 21 years\n* Pregnant women are excluded"
    assert exc == "* Does not consent to participate\n* Pregnant"


def test_bullet_prefixed_labels_with_colon():
    text = ("* INCLUSION CRITERIA:\n"
            "* Adults age \\>= 21 years\n"
            "\n"
            "EXCLUSION CRITERIA:\n"
            "\n"
            "* Prior surgical myectomy\n")
    inc, exc = _eligibility_split(text)
    assert inc == "* Adults age \\>= 21 years"
    assert exc == "* Prior surgical myectomy"


def test_inline_colon_labels():
    inc, exc = _eligibility_split(
        "Inclusion Criteria: adults with chronic heart failure\n"
        "Exclusion Criteria: pregnancy")
    assert inc == "adults with chronic heart failure"
    assert exc == "pregnancy"


def test_chinese_full_width_colon_labels():
    inc, exc = _eligibility_split("入选标准：慢性心力衰竭患者\n排除标准：严重肝肾功能不全")
    assert inc == "慢性心力衰竭患者"
    assert exc == "严重肝肾功能不全"


def test_no_labels_falls_back_to_inclusion():
    inc, exc = _eligibility_split("Adults 18-65 with heart failure")
    assert inc == "Adults 18-65 with heart failure"
    assert exc == "-"


def test_empty_criteria():
    assert _eligibility_split(None) == ("-", "-")
    assert _eligibility_split("") == ("-", "-")


def test_mid_sentence_exclusion_mention_does_not_split():
    # 'exclusion criteria' without a colon mid-sentence is not a section label
    inc, exc = _eligibility_split(
        "Patients with no exclusion criteria documented were enrolled\n"
        "Inclusion Criteria: adults\n"
        "Exclusion Criteria: pregnancy")
    assert inc == "adults"
    assert exc == "pregnancy"
