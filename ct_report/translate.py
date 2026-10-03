"""ct_report.translate — EN->ZH translation: cache, LLM batch translation, dictionary fallback."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional
from ct_report.dictionaries import _EN_LEFTOVERS_LIST, _EN_WORD_RE, _MEDICAL_ZH
from ct_report.textutil import _safe_text
import logging

logger = logging.getLogger(__name__)


_TRANSLATION_CACHE: dict[str, str] = {}

# Texts whose cached translation came from the dictionary fallback, not the
# LLM.  They stay in the in-memory cache for the current run but must never
# be persisted — otherwise one run without DEEPSEEK_API_KEY would pin every
# text to dictionary quality forever (re-translated only by deleting the
# cache file).
_DICT_ONLY: set[str] = set()


_TRANSLATION_CACHE_PATH = Path.home() / ".cache" / "ct_monitor_translate.json"


def _load_translation_cache() -> None:
    """加载本地翻译缓存."""
    global _TRANSLATION_CACHE
    if _TRANSLATION_CACHE_PATH.exists():
        try:
            with open(_TRANSLATION_CACHE_PATH, "r") as f:
                _TRANSLATION_CACHE = json.load(f)
        except (json.JSONDecodeError, OSError):
            _TRANSLATION_CACHE = {}


def _save_translation_cache() -> None:
    """保存翻译缓存到本地（仅 LLM 质量的条目，字典回退不落盘）."""
    try:
        _TRANSLATION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRANSLATION_CACHE_PATH, "w") as f:
            json.dump({k: v for k, v in _TRANSLATION_CACHE.items()
                       if k not in _DICT_ONLY}, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _batch_translate_llm(texts: list[str]) -> dict[str, str]:
    """用 DeepSeek LLM 批量翻译英文临床试验文本为中文."""
    # 去重 + 排除已缓存
    unique = list(dict.fromkeys(texts))
    uncached = [t for t in unique if t not in _TRANSLATION_CACHE]
    result = {t: _TRANSLATION_CACHE[t] for t in texts if t in _TRANSLATION_CACHE}

    if not uncached:
        return result

    # API key: DEEPSEEK_API_KEY is the documented name; fall back to
    # ANTHROPIC_AUTH_TOKEN for backwards compatibility with existing setups.
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        logger.warning(
            "No DEEPSEEK_API_KEY (or ANTHROPIC_AUTH_TOKEN) set — "
            "LLM translation disabled, using dictionary fallback only."
        )
        for t in uncached:
            zh = _clean_translation(_dict_translate(t))
            _TRANSLATION_CACHE[t] = zh
            _DICT_ONLY.add(t)
            result[t] = zh
        return result

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com/v1",
        )
    except Exception:
        logger.warning("OpenAI SDK not available, falling back to dict translation")
        for t in uncached:
            zh = _dict_translate(t)
            _TRANSLATION_CACHE[t] = zh
            _DICT_ONLY.add(t)
            result[t] = zh
        return result

    # 分批发送（每批 10 条）
    batch_size = 10
    llm_ok: set[str] = set()
    for i in range(0, len(uncached), batch_size):
        batch = uncached[i : i + batch_size]
        # Use numbered list format — avoids JSON key escaping issues
        lines = []
        for idx, t in enumerate(batch):
            # Strip special chars that confuse the LLM
            cleaned_t = t.replace("\n", " ").replace("\r", " ").replace('"', "'").replace("\\", "/")[:800]
            lines.append(f"{idx+1}. {cleaned_t}")
        prompt = (
            "Translate these clinical trial text fragments to PURE Chinese.\n"
            "Every single English word MUST be translated — absolutely no mixing.\n\n"
            "EXAMPLES:\n"
            'EN: "Patients with acute myocardial infarction"\n'
            'ZH: "急性心肌梗死患者"\n\n'
            'EN: "Ages 18-75 years old"\n'
            'ZH: "18-75岁"\n\n'
            'EN: "Exclusion criteria: 1. Pregnant women; 2. Severe infection"\n'
            'ZH: "排除标准：1. 孕妇；2. 严重感染"\n\n'
            'EN: "informed consent form signed"\n'
            'ZH: "已签署知情同意书"\n\n'
            "RULES:\n"
            "- Keep standard abbreviations: ECG, MRI, PCI, CABG, ACEI, BMI, LVEF, NYHA\n"
            "- Keep numbers and units: mg, ml, kg, cm, mmHg, %, years, months, weeks, days, hours\n\n"
            "Return ONLY a JSON array of strings, one per item:\n"
            '["translation1", "translation2", ...]\n\n'
            "Texts to translate:\n" + "\n".join(lines)
        )
        parsed = None
        for attempt in range(2):  # one retry, then dictionary fallback
            try:
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=4000,
                    timeout=120,
                )
                reply = resp.choices[0].message.content.strip()
                # Extract JSON from response (handle markdown code fences)
                if "```" in reply:
                    reply = reply.split("```")[1]
                    if reply.startswith("json"):
                        reply = reply[4:]
                parsed = json.loads(reply)
                break
            except Exception as e:
                logger.warning(
                    "LLM batch translation failed (batch %d, attempt %d/2): %s",
                    i // batch_size + 1, attempt + 1, e,
                )
        if isinstance(parsed, list):
            for t, zh in zip(batch, parsed):
                if t in uncached:
                    cleaned = _clean_translation(str(zh))
                    _TRANSLATION_CACHE[t] = cleaned
                    _DICT_ONLY.discard(t)
                    llm_ok.add(t)
                    result[t] = cleaned
        else:
            # Fallback: translate each text with the dictionary
            for t in batch:
                if t not in _TRANSLATION_CACHE:
                    zh = _clean_translation(_dict_translate(t))
                    _TRANSLATION_CACHE[t] = zh
                    _DICT_ONLY.add(t)
                    result[t] = zh

    if llm_ok:
        _save_translation_cache()
    # 确保所有请求的文本都有结果（字典回退仅驻内存，不落盘）
    for t in texts:
        if t not in result:
            zh = _clean_translation(_dict_translate(t))
            _TRANSLATION_CACHE[t] = zh
            _DICT_ONLY.add(t)
            result[t] = zh
    return result


def _clean_translation(text: str) -> str:
    """Post-process LLM translation: clean up English leftovers and normalize spaces."""
    if not text:
        return text
    # Replace English leftovers with Chinese equivalents
    def _replace(m: re.Match) -> str:
        word = m.group(1).lower()
        for eng, zh in _EN_LEFTOVERS_LIST:
            if word == eng.lower():
                return zh
        return m.group(0)  # should not happen
    text = _EN_WORD_RE.sub(_replace, text)
    # Clean up multiple spaces
    text = re.sub(r' +', ' ', text)
    # Clean up "的的", "和和" etc
    text = re.sub(r'([的与和或及是])\1+', r'\1', text)
    return text.strip()


def _dict_translate(text: Optional[str]) -> Optional[str]:
    """词典回退翻译——用于 LLM 不可用时."""
    if not text or text == "-":
        return text

    result = text
    # Case-insensitive phrase replacement (longest first)
    ci_map: dict[str, str] = {}
    for eng, zh in _MEDICAL_ZH.items():
        key = eng.lower()
        if key not in ci_map:
            ci_map[key] = zh
    # Also add some general words as fallback
    _FALLBACK_WORDS = {
        "the": "", "a": "", "an": "", "and": "和", "or": "或",
        "in": "在", "for": "对于", "with": "伴", "of": "的",
        "to": "至", "by": "通过", "from": "来自", "on": "在",
        "is": "是", "are": "是", "was": "是", "were": "是",
        "be": "是", "been": "是", "not": "非", "no": "无",
        "patients": "患者", "study": "研究", "trial": "试验",
        "treatment": "治疗", "therapy": "治疗", "effect": "效应",
        "effects": "效应", "efficacy": "疗效", "safety": "安全性",
        "versus": "对比", "vs": "对比", "phase": "期",
        "using": "使用", "based": "基于", "compared": "比较",
        "combined": "联合", "combination": "联合",
        "management": "管理", "outcomes": "结局",
        "clinical": "临床", "randomized": "随机",
        "controlled": "对照", "double-blind": "双盲",
        "acute": "急性", "chronic": "慢性", "severe": "重度",
        "primary": "主要", "secondary": "次要",
        "total": "总计", "overall": "总体",
        "short": "短期", "long": "长期", "term": "期",
        "follow-up": "随访", "follow up": "随访",
    }
    for eng, zh in _FALLBACK_WORDS.items():
        ci_map[eng] = zh

    sorted_phrases = sorted(ci_map.keys(), key=lambda x: -len(x))
    for phrase_lower in sorted_phrases:
        zh = ci_map[phrase_lower]
        if not zh:
            continue
        # Use word boundaries for single words, substring for phrases
        if " " in phrase_lower or len(phrase_lower) < 3:
            pattern = re.compile(r"\b" + re.escape(phrase_lower) + r"\b", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(phrase_lower), re.IGNORECASE)
        result = pattern.sub(zh, result)

    # Phase 2: remaining untranslated words
    words = re.findall(r"\b[a-zA-Z]+\b", result)
    for word in words:
        if any("\u4e00" <= c <= "\u9fff" for c in word):
            continue
        wl = word.lower()
        if wl in ci_map:
            result = re.sub(r"\b" + re.escape(word) + r"\b", ci_map[wl], result)

    result = re.sub(r"\s+", " ", result).strip()
    return result


def _translate_zh(text: Optional[str], max_len: int = 200) -> Optional[str]:
    """Translate English medical text to Chinese (cached LLM + dict fallback)."""
    if not text or text == "-":
        return text
    # Check cache first
    if text in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[text][:max_len] if max_len else _TRANSLATION_CACHE[text]
    # Dict fallback
    zh = _dict_translate(text)
    _TRANSLATION_CACHE[text] = zh or text
    return (zh or text)[:max_len] if max_len else (zh or text)


def _unwrap_json_list(text: str) -> str:
    """Turn a JSON array string into '; '-joined plain text (for display).

    List-type fields (conditions/interventions/sponsors/…) are stored as
    JSON arrays; translation and plain-text rendering work on readable text.
    Non-JSON input is returned unchanged.
    """
    if text.strip().startswith("["):
        try:
            items = json.loads(text)
            if isinstance(items, list):
                return "; ".join(str(i) for i in items if i)
        except (json.JSONDecodeError, TypeError):
            pass
    return text


def _zh_html(text: Optional[str], max_len: int = 200) -> str:
    """Return Chinese-only translation."""
    if not text or text == "-":
        return '<span class="na">-</span>'
    zh = _translate_zh(_unwrap_json_list(text))
    return _safe_text(zh, max_len)


def _translation_cache() -> dict:
    """Live view of the shared translation cache dict.

    generate_report (report.py) checks membership before
    queueing LLM translation; going through this accessor keeps
    the mutable global in one module.
    """
    return _TRANSLATION_CACHE
