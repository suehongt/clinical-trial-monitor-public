"""Bilingual (中/EN) terminology map for cross-language trial search.

Why this exists: the registries are linguistically split — NCT content is
English-only, CTR is Chinese-only, ChiCTR is bilingual, and the local query
evaluator (core.monitors.matching_trials) is plain substring matching. A
single-language query therefore misses the other language's records.  The
glossary lets the search API merge a query with its translation and lets the
live-check route each source a language its index understands.

Scope decisions:
  - Exact whole-phrase lookup only (the trimmed query must BE a glossary
    term).  Partial matches inside longer phrases are never expanded —
    unpredictable hybrid queries are worse than an honest miss.
  - Expansion lives in the search/live-check endpoints only, never inside
    matching_trials: saved monitors must keep the exact semantics they were
    saved with.
  - Static seed covering the monitored domain vocabulary (cardiology
    profiles + cardiac biomarkers, aligned with config.DISEASE_PROFILES
    report_keywords).  Extend the dict as new topics are tracked.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

ZH_TO_EN: dict[str, str] = {
    # 心肌病谱系（cmp profile 关键词）
    "心肌病": "cardiomyopathy",
    "肥厚型心肌病": "hypertrophic cardiomyopathy",
    "扩张型心肌病": "dilated cardiomyopathy",
    "限制型心肌病": "restrictive cardiomyopathy",
    "围产期心肌病": "peripartum cardiomyopathy",
    "应激性心肌病": "stress cardiomyopathy",
    "致心律失常性心肌病": "arrhythmogenic cardiomyopathy",
    # 冠脉 / 心梗（mi profile）
    "心肌梗死": "myocardial infarction",
    "心梗": "myocardial infarction",
    "冠脉综合征": "coronary syndrome",
    "急性冠脉综合征": "acute coronary syndrome",
    "急性心肌梗死": "acute myocardial infarction",
    # 心衰（hf profile）
    "心力衰竭": "heart failure",
    "心衰": "heart failure",
    # 血液肿瘤（mm profile）
    "多发性骨髓瘤": "multiple myeloma",
    "骨髓瘤": "myeloma",
    "浆细胞骨髓瘤": "plasma cell myeloma",
    "浆细胞白血病": "plasma cell leukemia",
    # 心肌炎及常用生物标志物
    "心肌炎": "myocarditis",
    "肌钙蛋白": "troponin",
    "利钠肽": "natriuretic peptide",
    "脑钠肽": "bnp",
    # 结直肠肿瘤谱系 + 表观遗传标志物（compound 拆词词汇，见 segment()）
    "结直肠癌": "colorectal cancer",
    "甲基化": "methylation",
}

EN_TO_ZH: dict[str, str] = {}
for _zh, _en in ZH_TO_EN.items():
    # first-wins：同一英文词有多个中文写法时（心衰/心力衰竭），保留
    # 词典里靠前的正式全称作为规范译文
    EN_TO_ZH.setdefault(_en, _zh)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# ── 检索结果挖掘（learned glossary，v19 glossary_terms） ────────────────────
#
# 原理：ChiCTR 式记录同一条里常带中英双字段（title/scientific_title），一条
# 记录就是一对翻译证据。用户检索词命中 K≥MIN_EVIDENCE 条这样的记录时，跨
# 记录做 n-gram 对齐：中文词看配对英文里最高频词组，英文词看配对中文里最
# 长公共子串。学习到的词对写入 glossary_terms（evidence≥2 自动 active 生效），
# CLI（scripts/glossary.py）可审查/退休，避免坏词对污染检索。
#
# 学习只在检索端点运行、且只对术语表查不到的词触发；matching_trials 本体
# 不动，saved monitor 语义不受影响。

LEARN_MIN_QUERY_LEN = 2
LEARN_MAX_QUERY_LEN = 40
LEARN_EVIDENCE_TO_ACTIVATE = 2

# 英文侧排除的泛词（词组对齐时的高频噪声）
_EN_STOPWORDS = frozenset({
    "study", "clinical", "patients", "patient", "trial", "trials", "randomized",
    "controlled", "treatment", "therapy", "efficacy", "safety", "diagnostic",
    "evaluation", "early", "acute", "chronic", "severe", "associated", "risk",
    "in", "of", "for", "and", "the", "a", "an", "with", "on", "by", "to", "at",
})
# 中文侧排除的泛词（公共子串对齐时的高频噪声）
_ZH_STOPWORDS = frozenset({
    "研究", "临床", "患者", "试验", "治疗", "分析", "评价", "观察", "疗效",
    "相关", "应用", "检测", "诊断", "急性", "慢性", "早期", "效果", "影响",
    "随机", "对照", "多中心", "前瞻性", "回顾性",
})


def lookup_learned(conn, text: Optional[str]) -> Optional[str]:
    """查已生效（active）的学习词条，双向精确匹配，静态表之前优先。

    学习词条更具体（如“高敏肌钙蛋白”），命中时优先于静态词典返回。
    """
    key = (text or "").strip().lower()
    if not key or conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT zh_term, en_term FROM glossary_terms "
            "WHERE status = 'active' AND "
            "(lower(zh_term) = ? OR lower(en_term) = ?) LIMIT 1",
            (key, key),
        ).fetchone()
    except Exception:
        return None  # 表尚未迁移时静默降级到静态词典
    if row is None:
        return None
    return row["en_term"] if has_cjk(key) else row["zh_term"]


def _sibling_pairs(conn, query: str) -> list:
    """按注册号找跨库兄弟记录的 (zh_title, en_title, record_id) 翻译配对。

    真实证据形态：ChiCTR 记录的中文标题与 ICTRP 同注册号记录的英文标题
    （source_trial_id 相同、语言不同）。ChiCTR 详情页同记录字段是中英混写
    而非成对翻译，所以不能取同记录字段配对。
    """
    if has_cjk(query):
        sql = """
            SELECT r.title AS zh_title, sib.title AS en_title,
                   r.record_id AS rid
            FROM registry_records r
            JOIN registry_records sib ON sib.source_trial_id = r.source_trial_id
                 AND sib.record_id != r.record_id AND sib.is_latest = 1
            WHERE r.is_latest = 1 AND instr(r.title, ?) > 0
              AND sib.title GLOB '*[a-zA-Z]*'
              AND sib.title NOT GLOB '*[一-龥]*'
            LIMIT 50"""
        rows = conn.execute(sql, (query,)).fetchall()
    else:
        sql = """
            SELECT sib.title AS zh_title, r.title AS en_title,
                   r.record_id AS rid
            FROM registry_records r
            JOIN registry_records sib ON sib.source_trial_id = r.source_trial_id
                 AND sib.record_id != r.record_id AND sib.is_latest = 1
            WHERE r.is_latest = 1 AND instr(lower(r.title), ?) > 0
              AND r.title GLOB '*[a-zA-Z]*' AND r.title NOT GLOB '*[一-龥]*'
              AND sib.title GLOB '*[一-龥]*'
            LIMIT 50"""
        rows = conn.execute(sql, (query.lower(),)).fetchall()
    return [(row["zh_title"], row["en_title"], row["rid"]) for row in rows]


def _en_candidates(en_texts: list) -> list:
    """统计英文文本集合里的词组 n-gram（1-4 词），按 (跨记录频次, 长度) 排序。

    返回 [(phrase, n_texts)]：n_texts 是出现该词组的**不同文本**数——同一
    文本里重复出现只计一次，避免单条记录的写作习惯污染候选排序。
    """
    counts: dict = {}
    for text in en_texts:
        words = [w.strip(".,;:()[]/\"'") for w in (text or "").lower().split()]
        words = [w for w in words if w]
        seen_in_text: set = set()
        for n in range(1, 5):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                if gram[0] in _EN_STOPWORDS or gram[-1] in _EN_STOPWORDS:
                    continue  # 边界词是泛词的词组多半是“… of patients”式噪声
                if n > 1 and any(w in _EN_STOPWORDS for w in gram[1:-1]):
                    continue
                phrase = " ".join(gram)
                if len(phrase) < 3 or len(phrase) > 60:
                    continue
                seen_in_text.add(phrase)
        for phrase in seen_in_text:
            counts.setdefault(phrase, set()).add(id(text))
    ranked = sorted(((phrase, len(ids)) for phrase, ids in counts.items()),
                    key=lambda kv: (-kv[1], -len(kv[0])))
    return ranked


def _zh_candidates(zh_texts: list) -> list:
    """在中文文本集合里找跨全部文本的公共子串，按长度降序，剔除纯泛词。"""
    if not zh_texts:
        return []
    shortest = min(zh_texts, key=len)
    candidates = []
    for width in range(len(shortest), 1, -1):
        for start in range(len(shortest) - width + 1):
            sub = shortest[start:start + width]
            if sub in _ZH_STOPWORDS or sub in candidates:
                continue
            if all(sub in t for t in zh_texts):
                candidates.append(sub)
    return candidates


def learn_from_results(conn, query: str) -> Optional[dict]:
    """从同注册号跨库兄弟记录（ChiCTR 中文 ↔ ICTRP 英文）学习译词。

    只对静态词典与已学词条都查不到的词触发。要求 ≥2 对兄弟标题配对，
    且英文候选词组必须跨 ≥2 条不同记录出现——单条记录的措辞习惯不足
    以立词。返回 {zh_term, en_term, evidence, status, term_id} 或 None。
    """
    query = (query or "").strip()
    if not (LEARN_MIN_QUERY_LEN <= len(query) <= LEARN_MAX_QUERY_LEN):
        return None
    if translate(query) or lookup_learned(conn, query):
        return None  # 已有译文，无需学习

    pairs = _sibling_pairs(conn, query)
    if len(pairs) < LEARN_EVIDENCE_TO_ACTIVATE:
        return None

    record_ids = [rid for _, _, rid in pairs]
    if has_cjk(query):
        picked = next((phrase for phrase, n_texts in
                       _en_candidates([en.lower() for _, en, _ in pairs])
                       if n_texts >= 2), None)
        en_term, zh_term = picked, query
    else:
        picked = next((sub for sub in _zh_candidates(
            [zh for zh, _, _ in pairs])), None)
        en_term, zh_term = query.lower(), picked
    if not en_term or not zh_term:
        return None
    if has_cjk(zh_term) and zh_term in _ZH_STOPWORDS:
        return None
    evidence = len(pairs)
    status = "active" if evidence >= LEARN_EVIDENCE_TO_ACTIVATE else "candidate"

    existing = conn.execute(
        "SELECT term_id, evidence FROM glossary_terms "
        "WHERE zh_term = ? AND en_term = ?", (zh_term, en_term)).fetchone()
    if existing:
        conn.execute(
            "UPDATE glossary_terms SET evidence = evidence + ?, "
            "last_seen_at = datetime('now'), "
            "status = CASE WHEN evidence + ? >= ? "
            "THEN 'active' ELSE status END WHERE term_id = ?",
            (evidence, evidence, LEARN_EVIDENCE_TO_ACTIVATE,
             existing["term_id"]))
        term_id = existing["term_id"]
    else:
        cur = conn.execute(
            "INSERT INTO glossary_terms (zh_term, en_term, source, status, "
            "evidence, example_record_ids, last_seen_at) "
            "VALUES (?, ?, 'learned', ?, ?, ?, datetime('now'))",
            (zh_term, en_term, status, evidence,
             json.dumps([r for r in record_ids if r is not None],
                        ensure_ascii=False)))
        term_id = cur.lastrowid
    conn.commit()
    learned = {"zh_term": zh_term, "en_term": en_term,
               "evidence": evidence, "status": status, "term_id": term_id}
    logger.info("glossary learn: %s ↔ %s (evidence=%d, %s)",
                zh_term, en_term, evidence, status)
    return learned


def has_cjk(text: Optional[str]) -> bool:
    """True when the text contains at least one CJK ideograph."""
    return bool(_CJK_RE.search(text or ""))


# ── 查询意图分类（原站检索字段路由）───────────────────────────────────────
#
# 背景（2026-09-29「罗氏」案例）：两个中国源的原站检索字段语义不同——
# CTR 通用框 keywords 匹配药物名称/申办者类字段但**不匹配**适应症，疾病
# 词必须走二级查询 indication（2026-09-24 实测）；ChiCTR searchproj 的
# title 只搜注册题目，申办者要走 secsponsor（试验主办单位）。此前按
# 「是否中文」一刀切路由：申办者词（罗氏）被送进 CTR 适应症而 0 命中，
# ChiCTR 则把标题含「罗氏」的无关词（罗氏菌/皮罗氏序列征）当申办者结果。
# 分类器按词形 + 本地库证据识别查询意图，供采集器选字段。

# 申办者/机构名特征：中文机构后缀（子串即可，中文无词边界）+
# 英文公司/机构词（词边界，防 "incidence" 式误配）
_SPONSOR_ZH_RE = re.compile(
    r"公司|药业|制药|医药|集团|医院|大学|学院|研究院|研究所|实验室")
_SPONSOR_LATIN_RE = re.compile(
    r"\b(?:gmbh|ltd|limited|inc|llc|corp|corporation|company|"
    r"pharma(?:ceutical|ceuticals)?|laborator(?:y|ies)|diagnostics|"
    r"institute|university|hospital|foundation)\b",
    re.IGNORECASE)


def has_sponsor_marker(text: Optional[str]) -> bool:
    """查询词带公司/机构名特征词（强申办者信号）。

    「上海罗氏制药有限公司」「Roche Diagnostics GmbH」这类词形即判；
    特征词选得保守——中文侧放弃「诊断」「生物」等易撞疾病查询的词
    （诊断试验/生物标志物），宁缺勿滥。
    """
    key = (text or "").strip()
    if not key:
        return False
    return bool(_SPONSOR_ZH_RE.search(key) or _SPONSOR_LATIN_RE.search(key))


def sponsors_in_library(text: Optional[str], conn=None) -> bool:
    """本地库 registry_records.sponsors 已有含该词的申办者（弱信号）。

    「罗氏」这类无特征词的简称靠这条识别——本地 CTR 记录的申办者是
    罗氏(中国)投资有限公司，说明该词在本域语境里是申办者名。子串 LIKE
    扫描在几万行规模下毫秒级；表缺失/连接不可用时静默 False（分类退化
    为词形判断，不影响可用性）。单字词不做库证——命中太泛，几乎必误判。
    """
    key = (text or "").strip()
    if not key or len(key) < 2 or conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM registry_records "
            "WHERE is_latest = 1 AND sponsors LIKE ? LIMIT 1",
            (f"%{key}%",)).fetchone()
    except Exception:
        return False
    return row is not None


def is_condition_term(text: Optional[str], conn=None) -> bool:
    """查询词是术语表已知的疾病/标志物词（静态词典或已学词条，双向）。"""
    key = (text or "").strip()
    if not key:
        return False
    return bool(translate(key) or lookup_learned(conn, key))


def translate(text: Optional[str]) -> Optional[str]:
    """Whole-phrase exact glossary lookup, both directions.

    Returns the translated term (same letter case style as the glossary,
    lowercase English) or None when the phrase is not a known term.
    """
    key = (text or "").strip().lower()
    if not key:
        return None
    if has_cjk(key):
        return ZH_TO_EN.get(key)
    return EN_TO_ZH.get(key)


# ── 中文复合词拆分（compound segmentation） ────────────────────────────────
#
# 动机：中文查询没有空格边界，“结直肠癌甲基化”在本地子串匹配（
# matching_trials 无空格 → 整串一个 term）和 ChiCTR 原站 title 整串检索
# 里都是 0 命中，而“结直肠癌”“甲基化”各自都能命中。segment() 只做
# 「词典全覆盖」的保守拆分：任何一段落不进词典（剩余碎词）就放弃——
# 宁可不拆，不发不可预测的混合查询。

# 贪心最长匹配一次成表；新词条加进 ZH_TO_EN 后自动参与
_SEG_VOCAB_SORTED = sorted(ZH_TO_EN.keys(), key=len, reverse=True)
_SEG_MAX_PARTS = 4


def _segment_vocab(conn) -> list:
    """静态词典 + 已生效学习词条（更具体的复合段优先），长词在前。"""
    vocab = list(_SEG_VOCAB_SORTED)
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT zh_term FROM glossary_terms WHERE status = 'active'"
            ).fetchall()
            vocab.extend(r["zh_term"] for r in rows)
        except Exception:
            pass  # 表尚未迁移时用静态词典即可
    return sorted(set(vocab), key=len, reverse=True)


def segment(text: Optional[str], conn=None) -> Optional[list]:
    """把无空格的中文复合词拆成词典已知术语序列，无法全覆盖则返回 None。

    词典 = 静态 ZH_TO_EN + 已学习 active 词条，贪心最长匹配；要求恰好
    覆盖整串且至少拆出 2 段（能整词命中的词不需要拆）。带空格或含非
    CJK 字符的查询返回 None——空格查询在 matching_trials 里已有 AND
    语义，混合语言串交给原样直发。
    """
    raw = (text or "").strip()
    if not raw or not has_cjk(raw) or re.search(r"\s", raw):
        return None
    if not all(_CJK_RE.fullmatch(ch) for ch in raw):
        return None
    segments: list = []
    # 贪心：每一步取能匹配当前位置的最长词典词
    vocab = _segment_vocab(conn)
    i = 0
    while i < len(raw):
        for term in vocab:
            if len(term) >= 2 and raw.startswith(term, i):
                segments.append(term)
                i += len(term)
                break
        else:
            return None  # 剩余碎词进不了词典 → 放弃拆分
    # 拆出过多段的多半不是有意义的复合词（词典词长串），不拆
    return segments if 2 <= len(segments) <= _SEG_MAX_PARTS else None


def translate_segments(text: Optional[str], conn=None) -> Optional[str]:
    """拆词并逐段翻译，全部段都有译文时返回空格连接的英文查询。

    “结直肠癌甲基化” → "colorectal cancer methylation"（NCT query.term
    按空格 AND）。任一段翻译不了就返回 None——丢段等于悄悄放宽语义，
    会把无关结果当作命中返回。
    """
    segments = segment(text, conn)
    if not segments:
        return None
    parts = []
    for seg in segments:
        en = lookup_learned(conn, seg) or translate(seg)
        if not en:
            return None
        parts.append(en)
    return " ".join(parts)
