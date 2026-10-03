"""core.terminology — bilingual glossary: static lookups + result mining."""
from __future__ import annotations

import pytest

from core import terminology
from db.connection import get_connection


def test_translate_zh_to_en():
    assert translate_zh_en("肌钙蛋白") == "troponin"
    assert translate_zh_en("心肌炎") == "myocarditis"
    assert translate_zh_en("心梗") == "myocardial infarction"
    assert translate_zh_en("多发性骨髓瘤") == "multiple myeloma"


def translate_zh_en(text):
    return terminology.translate(text)


def test_translate_en_to_zh():
    assert terminology.translate("Troponin") == "肌钙蛋白"
    assert terminology.translate("  heart failure  ") == "心力衰竭"
    assert terminology.translate("Myocarditis") == "心肌炎"


def test_translate_passthrough_and_misses():
    assert terminology.translate("not-a-glossary-term") is None
    assert terminology.translate("") is None
    assert terminology.translate(None) is None


def test_has_cjk():
    assert terminology.has_cjk("肌钙蛋白I") is True
    assert terminology.has_cjk("troponin I") is False
    assert terminology.has_cjk("") is False
    assert terminology.has_cjk(None) is False


# ── 检索结果挖掘（learned glossary） ───────────────────────────────────────


@pytest.fixture
def conn(test_db):
    return get_connection()


def _source_id(conn, short):
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short,)).fetchone()["source_id"]


def _seed_sibling_pairs(conn, pairs):
    """播种同注册号跨库兄弟对：ChiCTR 中文标题 + ICTRP 英文标题。"""
    chi, ict = _source_id(conn, "ChiCTR"), _source_id(conn, "ICTRP")
    for i, (zh_title, en_title) in enumerate(pairs):
        sid = f"ChiCTR26009{i:03d}"
        conn.execute(
            "INSERT INTO registry_records (source_id, source_trial_id, title,"
            " raw_payload) VALUES (?, ?, ?, '{}')", (chi, sid, zh_title))
        conn.execute(
            "INSERT INTO registry_records (source_id, source_trial_id, title,"
            " raw_payload) VALUES (?, ?, ?, '{}')", (ict, sid, en_title))
    conn.commit()


BILINGUAL_PAIRS = [
    ("心肌标志物在胸痛中的应用研究", "Cardiac biomarker panel in chest pain"),
    ("心肌标志物与预后评估", "Cardiac biomarker and prognosis"),
]


def test_learn_zh_query_activates_and_serves_lookup(conn):
    _seed_sibling_pairs(conn, BILINGUAL_PAIRS)
    learned = terminology.learn_from_results(conn, "心肌标志物")
    assert learned is not None
    assert learned["en_term"] == "cardiac biomarker"
    assert learned["status"] == "active"      # evidence=2 达到转正线
    assert terminology.lookup_learned(conn, "心肌标志物") == "cardiac biomarker"
    assert terminology.lookup_learned(conn, "Cardiac Biomarker") == "心肌标志物"
    # 静态词典查不到的词现在能翻译
    assert terminology.translate("心肌标志物") is None


def test_learn_en_query_extracts_zh_substring(conn):
    _seed_sibling_pairs(conn, BILINGUAL_PAIRS)
    learned = terminology.learn_from_results(conn, "cardiac biomarker")
    assert learned is not None
    assert learned["zh_term"] == "心肌标志物"
    assert learned["en_term"] == "cardiac biomarker"


def test_learn_rejected_below_evidence(conn):
    _seed_sibling_pairs(conn, BILINGUAL_PAIRS[:1])   # 只有 1 对 → 证据不足
    learned = terminology.learn_from_results(conn, "心肌标志物")
    assert learned is None
    assert terminology.lookup_learned(conn, "心肌标志物") is None


def test_learn_skips_terms_already_translated(conn):
    # 静态词典已有：不学习、不写库
    _seed_sibling_pairs(conn, [
        ("肌钙蛋白I检测研究", "Troponin I assay study"),
        ("肌钙蛋白T预后研究", "Troponin T prognosis study"),
    ])
    learned = terminology.learn_from_results(conn, "肌钙蛋白")
    assert learned is None
    n = conn.execute("SELECT COUNT(*) AS n FROM glossary_terms").fetchone()["n"]
    assert n == 0


def test_learn_skips_once_learned(conn):
    # 词对生效后再查同词：lookup 命中 → 不重复学习（单行不翻倍）
    _seed_sibling_pairs(conn, BILINGUAL_PAIRS)
    first = terminology.learn_from_results(conn, "心肌标志物")
    assert first is not None
    second = terminology.learn_from_results(conn, "心肌标志物")
    assert second is None
    n = conn.execute("SELECT COUNT(*) AS n FROM glossary_terms").fetchone()["n"]
    assert n == 1


# ── 查询意图分类（原站检索字段路由，2026-09-29） ───────────────────────────


def test_has_sponsor_marker_zh_company():
    assert terminology.has_sponsor_marker("上海罗氏制药有限公司") is True
    assert terminology.has_sponsor_marker("罗氏(中国)投资有限公司") is True
    assert terminology.has_sponsor_marker("北京协和医院") is True
    assert terminology.has_sponsor_marker("中国医学科学院肿瘤医院") is True


def test_has_sponsor_marker_latin_company():
    assert terminology.has_sponsor_marker("Roche Diagnostics GmbH") is True
    assert terminology.has_sponsor_marker("F. Hoffmann-La Roche Ltd") is True
    assert terminology.has_sponsor_marker("AstraZeneca AB") is False  # 未收 AB
    assert terminology.has_sponsor_marker("Pfizer Inc.") is True


def test_has_sponsor_marker_negative():
    # 疾病/标志物词不得误判（「诊断」「生物」等易撞词刻意未收）
    assert terminology.has_sponsor_marker("心肌炎") is False
    assert terminology.has_sponsor_marker("诊断试验") is False
    assert terminology.has_sponsor_marker("生物标志物") is False
    assert terminology.has_sponsor_marker("心力衰竭") is False
    assert terminology.has_sponsor_marker("") is False
    assert terminology.has_sponsor_marker(None) is False


def test_is_condition_term_static_and_miss():
    assert terminology.is_condition_term("心肌炎") is True
    assert terminology.is_condition_term("heart failure") is True
    assert terminology.is_condition_term("罗氏") is False
    assert terminology.is_condition_term("") is False


def test_is_condition_term_learned(conn):
    _seed_sibling_pairs(conn, BILINGUAL_PAIRS)
    assert terminology.learn_from_results(conn, "心肌标志物") is not None
    assert terminology.is_condition_term("心肌标志物", conn) is True


def test_sponsors_in_library(conn):
    assert terminology.sponsors_in_library("罗氏", conn) is False
    chi = _source_id(conn, "ChiCTR")
    conn.execute(
        "INSERT INTO registry_records (source_id, source_trial_id, title,"
        " raw_payload, sponsors, is_latest) VALUES (?, ?, ?, '{}', ?, 1)",
        (chi, "ChiCTR26999998", "种子", '["罗氏(中国)投资有限公司"]'))
    conn.commit()
    assert terminology.sponsors_in_library("罗氏", conn) is True
    assert terminology.sponsors_in_library("罗", conn) is False  # 单字不做库证
    assert terminology.sponsors_in_library("恒瑞", conn) is False


def test_sponsors_in_library_without_conn():
    # 无连接时静默 False（分类退化为词形判断，不影响可用性）
    assert terminology.sponsors_in_library("罗氏", None) is False
