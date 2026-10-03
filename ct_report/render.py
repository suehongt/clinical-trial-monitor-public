"""ct_report.render — HTML rendering helpers: badges, formatters, per-source sections."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from core.change_detection import FIELD_LABELS
from ct_report.constants import FIELD_QUALITY_MATRIX, SOURCE_COLORS, SOURCE_LABELS
from ct_report.textutil import _safe_text, esc
from ct_report.translate import _translate_zh, _zh_html


_CDE_HEADER = ["序号", "指标", "评价时间", "终点指标选择"]


def parse_cde_endpoint_table(text):
    """Rebuild structured rows from a flattened CDE endpoint table.

    CDE renders endpoints as a 4-column table (序号/指标/评价时间/终点指标
    选择); scraping flattens it to newline-joined cells.  Rows anchor on
    bare-integer 序号 lines; within a row the last two cells are 评价时间
    and 终点指标选择, everything between the 序号 and those is the multi-
    line 指标 cell.
    """
    if not text:
        return []
    lines = [l.strip() for l in str(text).split("\n") if l.strip()]
    lines = [re.sub(r"<[^>]+>", "", l).strip() for l in lines]
    lines = [l for l in lines if l]
    if lines[:4] == _CDE_HEADER:
        lines = lines[4:]
    starts = [i for i, l in enumerate(lines) if re.fullmatch(r"\d+", l)]
    if not starts:
        return []
    rows = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        cells = lines[start + 1:end]
        if len(cells) >= 3:
            indicator, time, choice = " ".join(cells[:-2]), cells[-2], cells[-1]
        elif len(cells) == 2:
            indicator, time, choice = cells[0], cells[1], ""
        elif len(cells) == 1:
            indicator, time, choice = cells[0], "", ""
        else:
            continue
        rows.append({"no": lines[start], "indicator": indicator,
                     "time": time, "type": choice})
    return rows


def format_cde_endpoint_table(rows) -> Optional[str]:
    """Render parsed CDE endpoint rows as an HTML table (None if empty)."""
    if not rows:
        return None
    head = "<tr><th>序号</th><th>指标</th><th>评价时间</th><th>终点指标选择</th></tr>"
    body = "".join(
        f"<tr><td class='no'>{_safe_text(r['no'])}</td>"
        f"<td>{_safe_text(r['indicator'])}</td>"
        f"<td>{_safe_text(r['time'])}</td>"
        f"<td>{_safe_text(r['type'])}</td></tr>"
        for r in rows)
    return f"<table class='cde-endpoint-table'>{head}{body}</table>"


def _registry_url(short_name: str, trial_id: str) -> str:
    """根据数据源和 trial_id 生成可点击的注册页面 URL。"""
    trial_id = esc(trial_id).strip()
    if short_name == "NCT":
        return f"https://clinicaltrials.gov/study/{trial_id}"
    elif short_name == "ChiCTR":
        return f"https://www.chictr.org.cn/show.html?regno={trial_id}"
    elif short_name == "CTR":
        return f"http://www.chinadrugtrials.org.cn/clinicaltrials.search.list.detail?registry_id={trial_id}"
    elif short_name == "ICTRP":
        return f"https://trialsearch.who.int/?TrialID={trial_id}"
    return "#"


def _status_badge(status: Optional[str], unknown_label: str = "未知") -> str:
    if not status:
        return f'<span class="badge badge-unknown">{esc(unknown_label)}</span>'

    status_lower = status.lower()
    if "recruit" in status_lower or "enroll" in status_lower:
        color = "#16a34a"
    elif "active" in status_lower or "进行中" in status_lower:
        color = "#ca8a04"
    elif "completed" in status_lower or "完成" in status_lower:
        color = "#6366f1"
    elif "suspend" in status_lower or "暂停" in status_lower:
        color = "#f59e0b"
    elif "terminat" in status_lower or "终止" in status_lower or "停止" in status_lower or "withdrawn" in status_lower or "撤回" in status_lower:
        color = "#dc2626"
    else:
        color = "#6b7280"

    return f'<span class="badge" style="background:{color}">{esc(status)}</span>'


def format_cross_source_badge(siblings: Optional[List[Dict]], english: bool = False) -> str:
    """Render chips linking to the sibling records of the same master trial.

    ``siblings`` items are {short_name, source_trial_id, source_url} from
    ct_report.query.fetch_cross_source_siblings().  Empty input returns "".
    """
    if not siblings:
        return ""
    label = "Also registered on: " if english else "跨源："
    chips = "".join(
        f'<a class="xsrc-chip" href="{esc(s.get("source_url") or "#")}" '
        f'title="{esc(s.get("source_trial_id", ""))}">{esc(s.get("short_name", "?"))}</a>'
        for s in siblings
    )
    return f'<div class="xsrc-line"><span class="xsrc-label">{esc(label)}</span>{chips}</div>'


def _source_badge(short_name: str) -> str:
    color = SOURCE_COLORS.get(short_name, "#6b7280")
    label = SOURCE_LABELS.get(short_name, short_name)
    return f'<span class="source-badge" style="background:{color}">{label}</span>'


def _format_endpoints(val: Optional[str]) -> str:
    """Format endpoint measures, handling both plain text and JSON arrays."""
    if not val or val == "-":
        return '<span class="na">-</span>'
    # Try JSON array
    if val.strip().startswith("["):
        try:
            items = json.loads(val)
            if isinstance(items, list):
                if not items:
                    # empty JSON array — render as N/A instead of a raw "[]"
                    return '<span class="na">-</span>'
                items_html = "".join(f"<li>{esc(item)}</li>" for item in items if item)
                if items_html:
                    return f'<ul class="endpoint-list">{items_html}</ul>'
                return '<span class="na">-</span>'
        except (json.JSONDecodeError, TypeError):
            pass
    # Plain text
    return _safe_text(val, 500)


def _format_list_json(json_str: Optional[str]) -> str:
    """Format a JSON array as an HTML list."""
    if not json_str:
        return '<span class="na">-</span>'
    try:
        items = json.loads(json_str)
        if isinstance(items, list):
            if not items:
                return '<span class="na">-</span>'
            items_list = "".join(f"<li>{esc(item)}</li>" for item in items if item)
            return f"<ul>{items_list}</ul>"
        return esc(str(items))
    except (json.JSONDecodeError, TypeError):
        return json_str or '<span class="na">-</span>'

def _format_sponsors(sponsors_json: Optional[str]) -> str:
    """Format sponsors JSON as readable text."""
    if not sponsors_json:
        return '<span class="na">-</span>'
    try:
        items = json.loads(sponsors_json)
        if isinstance(items, list):
            parts = []
            for item in items:
                if isinstance(item, dict) and "name" in item:
                    name = esc(item["name"])
                    role = item.get("role", "")
                    if role:
                        # Translate common roles
                        role_cn = {"lead": "牵头单位", "collaborator": "合作单位"}
                        role_str = role_cn.get(role, role)
                        parts.append(f"{name} <span class='sponsor-role'>({role_str})</span>")
                    else:
                        parts.append(name)
                elif isinstance(item, str):
                    # Short items are already plain names (e.g. CTR sponsors)
                    if len(item) <= 40:
                        parts.append(esc(item))
                    else:
                        # ChiCTR format: extract institution/hospital name
                        m = re.search(r'(?:单位\(医院\)[：:]\s*|Institution hospital[：:]\s*)([^\n]+)', item)
                        if m:
                            parts.append(esc(m.group(1).strip()))
                        else:
                            # Concise name from a long address block
                            m2 = re.search(r'[\u4e00-\u9fff]{2,}(?:医院|中心|学院|研究所|公司)', item)
                            parts.append(esc(m2.group(0) if m2 else item[:80]))
                else:
                    parts.append(esc(str(item)))
            return "<br>".join(parts) if parts else '<span class="na">-</span>'
        return esc(str(items))
    except (json.JSONDecodeError, TypeError):
        # Plain text (CTR format)
        text = sponsors_json.strip().replace("\t", " ").replace("/", " / ")
        return esc(text) or '<span class="na">-</span>'


_ELIG_INC_LABEL = r"(?:Inclusion\s+[Cc]riteria|Inclusion|入选标准)"
_ELIG_EXC_LABEL = r"(?:Exclusion\s+[Cc]riteria|Exclusion|排除标准)"


def _find_label_section(text: str, label: str):
    """First section-label occurrence as (start, content_start), or None.

    Two label forms: inline with a colon ('Exclusion criteria: ...'), and a
    standalone line as exported by ClinicalTrials.gov ('* INCLUSION
    CRITERIA' / 'EXCLUSION CRITERIA' — colon optional).  The colon-less
    form must own its line, so mid-sentence 'exclusion criteria' mentions
    never split the text.
    """
    patterns = (
        rf"{label}\s*[:：]\s*",
        rf"^[ \t]*(?:[*\-•][ \t]*)?{label}[ \t]*[:：]?[ \t]*$",
    )
    best = None
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end())
    return best


def _eligibility_split(criteria: Optional[str]) -> tuple[str, str]:
    """Split eligibility_criteria into inclusion and exclusion."""
    if not criteria:
        return "-", "-"

    inc = _find_label_section(criteria, _ELIG_INC_LABEL)
    exc = _find_label_section(criteria, _ELIG_EXC_LABEL)

    if exc and inc and inc[0] < exc[0]:
        inclusion = criteria[inc[1]:exc[0]]
        exclusion = criteria[exc[1]:]
    elif exc:
        # No usable inclusion label: text before the exclusion section is
        # still the inclusion criteria.
        inclusion = criteria[:exc[0]]
        exclusion = criteria[exc[1]:]
    elif inc:
        inclusion = criteria[inc[1]:]
        exclusion = ""
    else:
        # No labels at all: use entire text as inclusion
        inclusion = criteria
        exclusion = ""

    inclusion = inclusion.strip()[:2000]
    exclusion = exclusion.strip()[:2000]
    return inclusion or "-", exclusion or "-"


def _criteria_to_html(text: str) -> str:
    """将入选/排除标准文本解析为逐条 HTML 列表。"""
    if not text or text == "-":
        return '<span class="na">-</span>'

    # Normalize <br> tags to newlines for consistent processing
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Un-escape source-side escaped angle brackets ("\\<= 75") so they go
    # through the single html.escape pass below like every other character.
    text = text.replace('\\<', '<').replace('\\>', '>')

    # Try numbered items: "1. ..." or "1、..." or "1\n..."
    lines = text.split("\n")
    items: list[str] = []

    # Pattern 1: numbered items like "1. ...", "1、...", or "(1) ..."
    numbered = re.findall(r'(?:^|\n)\s*\(?\d+[.、）)\s]\s*(.*?)(?=\s*\n\s*\(?\d+[.、）)\s]|\s*$)', text, re.DOTALL)
    if numbered and len(numbered) >= 2:
        items = [item.strip() for item in numbered if item.strip()]
    else:
        # Pattern 2: numbered where number is on its own line (CTR format)
        # "1\nsome text\n2\nmore text"
        ctr_items = re.findall(r'(?:^|\n)\d+\s*\n\s*(.*?)(?=\n\d+\s*\n|\n*$)', text, re.DOTALL)
        if ctr_items and len(ctr_items) >= 2:
            items = [item.strip().replace("\n", " ") for item in ctr_items if item.strip()]
        else:
            # Pattern 3: bullet points "* ..."
            bullets = re.findall(r'^\s*\*\s+(.*?)$', text, re.MULTILINE)
            if bullets and len(bullets) >= 2:
                items = [item.strip() for item in bullets if item.strip()]
            else:
                # Pattern 4: split by newlines, filter non-empty
                for line in lines:
                    line = line.strip().lstrip("-•·").strip()
                    if line and not line.startswith(("Inclusion", "Exclusion", "入选", "排除")):
                        items.append(line)

    if not items:
        # Fallback: return as single paragraph
        return f'<p class="criteria-text">{esc(text[:2000])}</p>'

    # Remove duplicates (ChiCTR sometimes stores items twice)
    seen = set()
    unique_items: list[str] = []
    for item in items:
        key = item[:50]  # compare first 50 chars
        if key not in seen:
            seen.add(key)
            unique_items.append(item)

    items_html = "".join(f"<li>{esc(item)}</li>" for item in unique_items)
    return f'<ul class="criteria-list">{items_html}</ul>'


# Trial-card field labels: key -> (中文, English).  The card renderer picks
# a pair by language; the rendered text must stay identical to the labels
# that were previously inline ternaries in ct_report.report.
_CARD_LABELS: Dict[str, tuple] = {
    "study_type": ("研究类型", "Study Type"),
    "phase": ("研究阶段", "Phase"),
    "start_date": ("开始日期", "Start Date"),
    "enrollment": ("入组人数", "Enrollment"),
    "last_updated": ("最后更新", "Last Updated"),
    "investigator": ("研究者", "Investigator"),
    "sponsor": ("申办方", "Sponsor"),
    "purpose": ("研究目的", "Purpose"),
    "condition": ("疾病/适应症", "Condition"),
    "primary_endpoint": ("主要终点", "Primary Endpoint"),
    "secondary_endpoints": ("次要终点", "Secondary Endpoints"),
    "unknown_status": ("未知", "Unknown"),
}


def _card_label(key: str, english: bool) -> str:
    """Pick one (中文, English) label pair for the card language."""
    zh, en = _CARD_LABELS[key]
    return en if english else zh


# ── NEW/CHANGED 标注（增量与 --force 报告共用） ────────────────────────

_EVENT_VALUE_MAX_CHARS = 60


def _format_event_value(val: Optional[str]) -> str:
    """压缩事件显示值：JSON 数组展开拼接，超长截断（转义前调用）。"""
    if val is None or str(val).strip() == "":
        return "—"
    try:
        parsed = json.loads(val)
        if isinstance(parsed, list):
            val = "、".join(
                v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                for v in parsed
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    val = str(val).strip()
    if len(val) > _EVENT_VALUE_MAX_CHARS:
        val = val[:_EVENT_VALUE_MAX_CHARS] + "…"
    return val


def _change_block_html(change_events: Optional[List[Dict[str, Any]]],
                       english: bool) -> str:
    """字段级 old→new 变更块；无事件返回空串。"""
    if not change_events:
        return ""
    lang = "en" if english else "zh"
    label_text = "Field Changes" if english else "本次变更"
    rows = ""
    for e in change_events:
        label = FIELD_LABELS.get(e["field_name"], {}).get(lang, e["field_name"])
        rows += (
            f"<div class='change-row'>"
            f"<span class='change-label'>{esc(label)}</span>"
            f"<span class='change-values'>"
            f"<span class='change-old'>{esc(_format_event_value(e.get('old_value')))}</span>"
            f" <span class='change-arrow'>→</span> "
            f"<span class='change-new'>{esc(_format_event_value(e.get('new_value')))}</span>"
            f"</span></div>"
        )
    return (f"<div class='change-block'>"
            f"<div class='change-block-title'>{label_text}</div>{rows}</div>")


def render_trial_card(
    t: Dict,
    *,
    status: str,
    study_type: str,
    enrollment_display: str,
    pi: str,
    purpose: str,
    inclusion: str,
    exclusion: str,
    sibling_chips_html: str = "",
    cde_primary_html: Optional[str] = None,
    cde_secondary_html: Optional[str] = None,
    is_new: bool = False,
    is_changed: bool = False,
    change_events: Optional[List[Dict[str, Any]]] = None,
    translate: bool = False,
    english: bool = False,
) -> str:
    """Render one trial card for the per-source report sections.

    The caller (ct_report.report.generate_report) computes every dynamic
    value (criteria split, PI/purpose extraction, CDE endpoint tables,
    cross-source sibling chips, enrollment display, translate/english
    flags); this function only escapes/formats and assembles the card
    HTML.  ``t`` is a row from ct_report.query.query_trials().

    ``is_new`` / ``is_changed`` / ``change_events`` carry the incremental
    baseline diff: NEW/CHANGED chips in the card header and a field-level
    old→new block under the title.
    """
    chips = ""
    if is_new:
        chips += f"<span class='chip chip-new'>{'NEW' if english else '新增'}</span>"
    if is_changed:
        chips += f"<span class='chip chip-changed'>{'CHANGED' if english else '变更'}</span>"
    change_block = _change_block_html(change_events, english)
    return f"""
                <div class="trial-card">
                    <div class="card-header">
                        <span class="trial-id"><a href="{_registry_url(t['short_name'], t['source_trial_id'])}" target="_blank" class="trial-link">{_safe_text(t['source_trial_id'])}</a></span>
                        {_status_badge(_translate_zh(status) if translate else status, unknown_label=_card_label("unknown_status", english))}
                        {chips}
                    </div>
                    <div class="card-body">
                        <h3 class="trial-title">{_zh_html(t.get('title') or t.get('scientific_title'), 200) if translate else _safe_text(t.get('title') or t.get('scientific_title'), 200)}</h3>
                        {change_block}
                        {sibling_chips_html}

                        <table class="field-table">
                            <tr>
                                <td class="field-label">{_card_label("study_type", english)}</td>
                                <td>{_zh_html(study_type) if translate else esc(study_type)}</td>
                                <td class="field-label">{_card_label("phase", english)}</td>
                                <td>{_zh_html(t.get('study_phase')) if translate else _safe_text(t.get('study_phase'))}</td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("start_date", english)}</td>
                                <td>{_safe_text(t.get('start_date') or t.get('registration_date'))}</td>
                                <td class="field-label">{_card_label("enrollment", english)}</td>
                                <td>{_safe_text(enrollment_display)}</td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("last_updated", english)}</td>
                                <td colspan="3">{_safe_text(t.get('last_updated_at_source') or t.get('last_crawled_at'))}</td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("investigator", english)}</td>
                                <td colspan="3">{_zh_html(pi) if translate else esc(pi)}</td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("sponsor", english)}</td>
                                <td colspan="3">{_zh_html(_format_sponsors(t.get('sponsors')), 500) if translate else _format_sponsors(t.get('sponsors'))}</td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("purpose", english)}</td>
                                <td colspan="3"><div class="purpose-text">{_zh_html(purpose, 2000) if translate else _safe_text(purpose, 2000)}</div></td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("condition", english)}</td>
                                <td colspan="3"><div class="conditions-tags">{_zh_html(t.get('conditions'), 300) if translate else _format_list_json(t.get('conditions'))}</div></td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("primary_endpoint", english)}</td>
                                <td colspan="3">{(cde_primary_html or (_zh_html(t.get('primary_endpoint'), 500) if translate else _format_endpoints(t.get('primary_endpoint'))))}</td>
                            </tr>
                            <tr>
                                <td class="field-label">{_card_label("secondary_endpoints", english)}</td>
                                <td colspan="3">{(cde_secondary_html or (_zh_html(t.get('secondary_endpoints'), 500) if translate else _format_endpoints(t.get('secondary_endpoints'))))}</td>
                            </tr>
                        </table>

                        <div class="criteria-section">
                            <div class="criteria-box">
                                <div class="criteria-label inclusion">入选标准</div>
                                {_criteria_to_html(inclusion)}
                            </div>
                            <div class="criteria-box">
                                <div class="criteria-label exclusion">排除标准</div>
                                {_criteria_to_html(exclusion)}
                            </div>
                        </div>

                        <div class="card-footer">
                            <a href="{_safe_text(t.get('source_url'), 500)}" target="_blank" class="source-link">查看原文 →</a>
                        </div>
                    </div>
                </div>
                """


def _extract_brief_summary(raw_payload: Optional[str]) -> Optional[str]:
    """从 NCT JSON payload 中提取 briefSummary。"""
    if not raw_payload or not raw_payload.strip().startswith("{"):
        return None
    try:
        data = json.loads(raw_payload)
        return data.get("protocolSection", {}).get("descriptionModule", {}).get("briefSummary")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _extract_investigator(raw_payload: Optional[str]) -> Optional[str]:
    """从 NCT JSON payload 中提取 investigator (overallOfficials)。"""
    if not raw_payload or not raw_payload.strip().startswith("{"):
        return None
    try:
        data = json.loads(raw_payload)
        officials = data.get("protocolSection", {}).get("contactsLocationsModule", {}).get("overallOfficials", [])
        if officials:
            # Prefer PRINCIPAL_INVESTIGATOR, then first available
            pi = next((o for o in officials if o.get("role") == "PRINCIPAL_INVESTIGATOR"), officials[0])
            name = pi.get("name", "")
            aff = pi.get("affiliation", "")
            if name and aff:
                return f"{name} ({aff})"
            return name or None
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return None


def _extract_ictrp_target_size(raw_payload: Optional[str]) -> Optional[str]:
    """从 ICTRP JSON payload 中提取完整的 target_size（含分组信息）。"""
    if not raw_payload or not raw_payload.strip().startswith("{"):
        return None
    try:
        data = json.loads(raw_payload)
        who_record = data.get("who_ictrp_record", {})
        ts = who_record.get("target_size", "")
        if ts and re.search(r":\d+", ts):
            return ts.strip()
        return None
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _extract_ictrp_criteria(raw_payload: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """从 ICTRP JSON payload 中分别提取 inclusion_criteria 和 exclusion_criteria。

    WHO ICTRP XML 本身包含独立的 <Inclusion_Criteria> 和 <Exclusion_Criteria> 字段，
    应直接使用而非从合并的 eligibility_criteria 中重新分割。
    Returns (inclusion, exclusion).
    """
    if not raw_payload or not raw_payload.strip().startswith("{"):
        return None, None
    try:
        data = json.loads(raw_payload)
        who_record = data.get("who_ictrp_record", {})
        inc = who_record.get("inclusion_criteria") or None
        exc = who_record.get("exclusion_criteria") or None
        # Strip redundant label prefixes from the raw text (e.g. "Inclusion criteria: 1. ..." → "1. ...")
        for prefix in ("Inclusion criteria:", "Inclusion Criteria:", "inclusion criteria:", "Inclusion:", "inclusion:"):
            if inc and inc.strip().startswith(prefix):
                inc = inc.strip()[len(prefix):].strip()
                break
        for prefix in ("Exclusion criteria:", "Exclusion Criteria:", "exclusion criteria:", "Exclusion:", "exclusion:"):
            if exc and exc.strip().startswith(prefix):
                exc = exc.strip()[len(prefix):].strip()
                break
        return inc, exc
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None, None


def _calc_target_size_total(raw_payload: Optional[str]) -> Optional[str]:
    """从 ICTRP target_size 计算总入组人数。

    对含分组描述（如 "Group A:30;Group B:170"）的 target_size 计算总和，
    返回 "总计: 200（Group A:30; Group B:170）" 格式。
    对于简单数字则返回 None。
    """
    if not raw_payload or not raw_payload.strip().startswith("{"):
        return None
    try:
        data = json.loads(raw_payload)
        who_record = data.get("who_ictrp_record", {})
        ts = who_record.get("target_size", "").strip()
        if not ts:
            return None
        # Check if it has group format (text:number; pattern)
        groups = re.findall(r'([^:;]+):\s*(\d+)', ts)
        if len(groups) >= 2:
            total = sum(int(g[1]) for g in groups)
            parts = "; ".join(f"{g[0].strip()}:{g[1]}" for g in groups)
            return f"总计: {total}（{parts}）"
        # Single number
        m = re.search(r'\d[\d,]*', ts.replace(',', ''))
        if m:
            return None  # Simple number, use enrollment field directly
        return ts  # Something else, show as-is
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


# Source columns of the 字段完整度矩阵, in display order (registry_sources.short_name).
_QUALITY_MATRIX_SOURCES = ("NCT", "CTR", "ChiCTR", "ICTRP")


def _coverage_cell(covered: int, total: int) -> str:
    """Format one field-coverage matrix cell: emoji tier + covered/total (pct%).

    Tiers: ✅ ≥95%, ⚠️ ≥50%, ❌ >0%, ➖ no coverage at all.
    """
    pct = (covered / total * 100.0) if total else 0.0
    if covered == 0:
        emoji = "➖"
    elif pct >= 95:
        emoji = "✅"
    elif pct >= 50:
        emoji = "⚠️"
    else:
        emoji = "❌"
    return f"{emoji} {covered}/{total} ({pct:.1f}%)"


def _render_quality_overview(by_source: Dict[str, List]) -> str:
    """Render a data quality methodology section (profile- and data-driven)."""
    from core.quality_checks import field_coverage_by_source
    from ct_report.constants import ACTIVE_PROFILE, SEARCH_KEYWORDS

    label = ACTIVE_PROFILE["label"]
    n_nct = len(by_source.get("NCT", []))
    n_chictr = len(by_source.get("ChiCTR", []))
    n_ctr = len(by_source.get("CTR", []))
    n_ictrp_native = len(by_source.get("ICTRP_native", []))
    n_ictrp_chictr = len(by_source.get("ICTRP_ChiCTR", []))
    n_ictrp = n_ictrp_native + n_ictrp_chictr
    keyword_list = "、".join(SEARCH_KEYWORDS)

    # Build quality matrix rows — computed live from the DB when possible,
    # falling back to the static hand-maintained FIELD_QUALITY_MATRIX when
    # the registry tables are unavailable so the panel never breaks.
    coverage = field_coverage_by_source()
    matrix_rows = ""
    if coverage:
        matrix_note = ("基于数据库 registry_records（is_latest=1）实时计算的字段覆盖度。"
                       "✅=≥95%，⚠️=≥50%，❌=>0%，➖=无覆盖；单元格为 覆盖数/总数 (百分比)。")
        field_labels = list(next(iter(coverage.values())))
        for field in field_labels:
            cells = "".join(
                f'<td class="fq-cell">'
                f'{_coverage_cell(*coverage.get(src, {}).get(field, (0, 0)))}'
                f'</td>'
                for src in _QUALITY_MATRIX_SOURCES
            )
            matrix_rows += f"""
        <tr>
            <td class="fq-field">{field}</td>
            {cells}
        </tr>"""
    else:
        matrix_note = "每个数据源的关键字段可用性对比。✅=可用，⚠️=部分可用（附完整率），❌=该数据源不提供。"
        for row_data in FIELD_QUALITY_MATRIX:
            field, nct, ctr, chictr, ictrp = row_data
            matrix_rows += f"""
        <tr>
            <td class="fq-field">{field}</td>
            <td class="fq-cell">{nct}</td>
            <td class="fq-cell">{ctr}</td>
            <td class="fq-cell">{chictr}</td>
            <td class="fq-cell">{ictrp}</td>
        </tr>"""

    return f"""
    <!-- Data Quality Methodology -->
    <div class="source-section">
        <div class="source-header" style="border-left: 4px solid #8b5cf6;">
            <h2>📋 数据质量与方法论说明</h2>
        </div>
        <div class="quality-card">
            <h3>数据筛选说明</h3>
            <ul>
                <li><strong>{label} 筛选</strong>：使用 {len(SEARCH_KEYWORDS)} 个关键词（{keyword_list}）在标题、科学标题、疾病/适应症字段中匹配，ASCII 关键词要求词边界命中以避免子串假阳性。</li>
                <li><strong>NCT (ClinicalTrials.gov)</strong>：通过 API v2 <code>query.cond</code>（{ACTIVE_PROFILE['query_cond']}）按疾病概念检索，本次报告匹配 <strong>{n_nct} 条</strong>。</li>
                <li><strong>ChiCTR</strong>：关键词搜索直爬 <strong>{n_chictr} 条</strong>；通过 ICTRP 间接获取 <strong>{n_ictrp_chictr} 条</strong>，合计 <strong>{n_chictr + n_ictrp_chictr} 条</strong>。直爬覆盖率受站点 WAF 与关键词覆盖限制。</li>
                <li><strong>CTR</strong>：关键词搜索直爬 <strong>{n_ctr} 条</strong>。</li>
                <li><strong>ICTRP (WHO)</strong>：XML 快照导入（AGGREGATOR，不计独立发现），其中 <strong>{n_ictrp} 条</strong>与本疾病关键词匹配。</li>
            </ul>

            <h3>字段完整度矩阵</h3>
            <p class="quality-note">{matrix_note}</p>
            <table class="quality-matrix">
                <thead>
                    <tr>
                        <th>字段</th>
                        <th>NCT</th>
                        <th>CTR</th>
                        <th>ChiCTR</th>
                        <th>ICTRP</th>
                    </tr>
                </thead>
                <tbody>
                    {matrix_rows}
                </tbody>
            </table>

            <h3>各源特点与局限</h3>
            <div class="quality-grid">
                <div class="quality-item">
                    <div class="quality-item-header" style="color:#2563eb;">NCT (ClinicalTrials.gov)</div>
                    <ul>
                        <li>字段最完整，含 Arm Group、Locations、Study Design</li>
                        <li>本次匹配 {n_nct} 条（query.cond 疾病概念检索）</li>
                        <li>适用：需要详细结构化字段时</li>
                    </ul>
                </div>
                <div class="quality-item">
                    <div class="quality-item-header" style="color:#16a34a;">ICTRP (WHO)</div>
                    <ul>
                        <li>覆盖最广（本次匹配 {n_ictrp} 条），涵盖 17+ 注册库</li>
                        <li>缺少 Study Design、Locations、Arm Group 等字段</li>
                        <li>Phase 值不规范（N/A、0、Retrospective 混用）</li>
                        <li>适用：宏观趋势分析、全球分布、跨库比较</li>
                    </ul>
                </div>
                <div class="quality-item">
                    <div class="quality-item-header" style="color:#dc2626;">ChiCTR (中国)</div>
                    <ul>
                        <li>本次直爬匹配 {n_chictr} 条（受 WAF 与关键词覆盖限制）</li>
                        <li>通过 ICTRP 间接获取 {n_ictrp_chictr} 条 ChiCTR 注册的试验</li>
                        <li>中英文双语数据，Study Design 和 Locations 是独有字段</li>
                        <li>受 WAF 限制直爬速度慢，需数小时间隔</li>
                    </ul>
                </div>
                <div class="quality-item">
                    <div class="quality-item-header" style="color:#ca8a04;">CTR (中国药监局)</div>
                    <ul>
                        <li>字段完整度高，含 Arm Group</li>
                        <li>缺少 Study Type 和 Study Design</li>
                        <li>Wangsu WAF 限制</li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
    """


_ictrp_labels_cn = {
    "trials": "{count} 项试验", "status_dist": "试验状态分布", "study_type": "研究类型",
    "phase": "研究阶段", "countries": "Top 10 国家/地区", "registries": "来源注册库",
    "conditions": "Top 10 适应症", "sponsors": "Top 10 申办方", "phase_col": "阶段",
    "enrollment": "入组", "condition": "条件", "sponsor": "申办方",
    "sample": "查看最近 20 条试验示例", "data_note": "ICTRP 数据说明",
    "data_note_text": ("ICTRP 为元注册库，数据来自全球 17+ 个注册库。"
                       "其 Study Phase 等字段为各注册库原始值，未做标准化映射。"
                       "入组人数为各注册库报告值，可能不完整。"),
    "sample_trials": "Sample Trials (Last 20)",
    "empty": "该数据源暂无匹配结果",
}


_ictrp_labels = {
    "trials": "{count} trials", "status_dist": "Status Distribution", "study_type": "Study Type",
    "phase": "Phase", "countries": "Top 10 Countries", "registries": "Source Registries",
    "conditions": "Top 10 Conditions", "sponsors": "Top 10 Sponsors", "phase_col": "Phase",
    "enrollment": "Enrollment", "condition": "Condition", "sponsor": "Sponsor",
    "sample": "Show Last 20 Sample Trials", "data_note": "ICTRP Data Notes",
    "data_note_text": ("ICTRP is a meta-registry aggregating data from 17+ global registries. "
                       "Study Phase values are raw registry values, not normalized. "
                       "Enrollment numbers are as reported by each registry and may be incomplete."),
    "sample_trials": "Sample Trials (Last 20)",
    "empty": "No matching trials",
}


def _render_ictrp_aggregate(trials: List[Dict], src_label: str, src_color: str, src_name: str = "ICTRP_native", translate: bool = False, english: bool = False) -> str:
    """Render WHO ICTRP as aggregate statistics + sample cards (not individual listing)."""
    count = len(trials)

    L = _ictrp_labels if english else _ictrp_labels_cn

    # ── Aggregate statistics ──
    statuses: Dict[str, int] = {}
    phases: Dict[str, int] = {}
    countries: Dict[str, int] = {}
    study_types: Dict[str, int] = {}
    conditions: Dict[str, int] = {}
    sponsors: Dict[str, int] = {}
    registries: Dict[str, int] = {}

    for t in trials:
        # Status
        st = t.get("status_label") or "Unknown"
        statuses[st] = statuses.get(st, 0) + 1

        # Study type
        sty = t.get("study_type_label") or "Unknown"
        study_types[sty] = study_types.get(sty, 0) + 1

        # Phase
        ph = t.get("study_phase") or "Unknown"
        phases[ph] = phases.get(ph, 0) + 1

        # Countries
        try:
            ctrs = json.loads(t["countries"]) if t.get("countries") else []
            for c in ctrs:
                countries[c] = countries.get(c, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

        # Conditions (sample top)
        raw_cond = t.get("conditions")
        if raw_cond:
            conds = None
            # Try JSON array first
            if raw_cond.strip().startswith("["):
                try:
                    conds = json.loads(raw_cond)
                except (json.JSONDecodeError, TypeError):
                    pass
            if conds is None:
                # Plain text: split by semicolons, newlines, or <br>
                raw = raw_cond.replace("<br>", ";").replace("<br/>", ";")
                raw = raw.replace("\n", ";").replace("\r", ";")
                conds = [c.strip() for c in raw.split(";") if c.strip()]
            for c in conds:
                conditions[c] = conditions.get(c, 0) + 1

        # Sponsors
        try:
            sps = json.loads(t["sponsors"]) if t.get("sponsors") else []
            for s in sps:
                if isinstance(s, dict) and "name" in s:
                    sponsors[s["name"]] = sponsors.get(s["name"], 0) + 1
                elif isinstance(s, str):
                    sponsors[s[:60]] = sponsors.get(s[:60], 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

        # Registry source (from trial ID pattern)
        sid = t["source_trial_id"]
        if sid.startswith("NCT"):
            reg = "ClinicalTrials.gov"
        elif sid.startswith("ChiCTR") or sid.startswith("CHICTR"):
            reg = "ChiCTR"
        elif sid.startswith("CTR"):
            reg = "CTR"
        elif sid.startswith("IRCT"):
            reg = "IRCT (Iran)"
        elif sid.startswith("JPRN"):
            reg = "JPRN (Japan)"
        elif sid.startswith("DRKS"):
            reg = "DRKS (Germany)"
        elif sid.startswith("ISRCTN"):
            reg = "ISRCTN (UK)"
        elif sid.startswith("ANZCTR") or sid.startswith("ACTRN"):
            reg = "ANZCTR (Australia/NZ)"
        elif sid.startswith("TCTR"):
            reg = "TCTR (Thailand)"
        elif sid.startswith("EUCTR"):
            reg = "EU Clinical Trials"
        elif sid.startswith("NL-OMON"):
            reg = "NL-OMON (Netherlands)"
        elif sid.startswith("ITMCTR"):
            reg = "ITMCTR"
        elif sid.startswith("CTRI"):
            reg = "CTRI (India)"
        else:
            reg = "Other"
        registries[reg] = registries.get(reg, 0) + 1

    # ── Build HTML ──
    # Status distribution
    status_rows = ""
    for s, n in sorted(statuses.items(), key=lambda x: -x[1]):
        pct = n / count * 100
        status_rows += f"""
        <tr><td>{_status_badge(s)}</td><td>{n}</td><td><div class="bar" style="width:{pct*2}px"></div></td></tr>"""

    # Phase distribution
    phase_rows = ""
    for p, n in sorted(phases.items(), key=lambda x: -x[1]):
        pct = n / count * 100
        phase_rows += f"<tr><td>{_safe_text(p, 40)}</td><td>{n}</td><td>{pct:.1f}%</td></tr>"

    # Top 10 countries
    top_countries = sorted(countries.items(), key=lambda x: -x[1])[:10]
    country_rows = ""
    for c, n in top_countries:
        pct = n / count * 100
        country_rows += f"<tr><td>{esc(c)}</td><td>{n}</td><td>{pct:.1f}%</td></tr>"

    # Study types
    type_rows = ""
    for st, n in sorted(study_types.items(), key=lambda x: -x[1]):
        pct = n / count * 100
        type_rows += f"<tr><td>{esc(st)}</td><td>{n}</td><td>{pct:.1f}%</td></tr>"

    # Top registries
    reg_rows = ""
    for r, n in sorted(registries.items(), key=lambda x: -x[1])[:10]:
        pct = n / count * 100
        reg_rows += f"<tr><td>{esc(r)}</td><td>{n}</td><td>{pct:.1f}%</td></tr>"

    # Top 10 conditions
    top_conditions = sorted(conditions.items(), key=lambda x: -x[1])[:10]
    cond_rows = ""
    for c, n in top_conditions:
        pct = n / count * 100
        cond_rows += f"<tr><td>{_safe_text(c, 60)}</td><td>{n}</td><td>{pct:.1f}%</td></tr>"

    # Top 10 sponsors
    top_sponsors = sorted(sponsors.items(), key=lambda x: -x[1])[:10]
    sp_rows = ""
    for s, n in top_sponsors:
        sp_rows += f"<tr><td>{_safe_text(s, 50)}</td><td>{n}</td></tr>"

    # ── Sample trials (last 20) ──
    sample_cards = ""
    for t in trials[:20]:
        status = t.get("status_label") or "-"
        sample_cards += f"""
        <div class="trial-card">
            <div class="card-header">
                <span class="trial-id"><a href="{_registry_url(t['short_name'], t['source_trial_id'])}" target="_blank" class="trial-link">{_safe_text(t['source_trial_id'])}</a></span>
                {_status_badge(status, unknown_label="Unknown" if english else "未知")}
            </div>
            <div class="card-body">
                <div class="trial-title">{_zh_html(t.get('title') or t.get('scientific_title'), 200) if translate else _safe_text(t.get('title') or t.get('scientific_title'), 200)}</div>
                <table class="field-table">
                    <tr><td class="field-label">{L["phase_col"]}</td><td>{_zh_html(t.get('study_phase')) if translate else _safe_text(t.get('study_phase'))}</td><td class="field-label">{L["enrollment"]}</td><td>{_safe_text(t.get('enrollment'))}</td></tr>
                    <tr><td class="field-label">{L["condition"]}</td><td colspan="3"><div class="conditions-tags">{_zh_html(t.get('conditions'), 300) if translate else _format_list_json(t.get('conditions'))}</div></td></tr>
                    <tr><td class="field-label">{L["sponsor"]}</td><td colspan="3">{_format_sponsors(t.get('sponsors'))}</td></tr>
                </table>
            </div>
        </div>"""

    count_label = L["trials"].format(count=count)
    return f"""
    <div class="source-section" data-source="{src_name}">
        <div class="source-header" style="border-left: 4px solid {src_color};">
            <h2>{src_label}</h2>
            <span class="count-badge">{count_label}</span>
        </div>

        <!-- Aggregate stats row -->
        <div class="aggregate-grid">
            <div class="agg-card">
                <h4>{L["status_dist"]}</h4>
                <table class="agg-table"><tbody>{status_rows}</tbody></table>
            </div>
            <div class="agg-card">
                <h4>{L["study_type"]}</h4>
                <table class="agg-table"><tbody>{type_rows}</tbody></table>
            </div>
            <div class="agg-card">
                <h4>{L["phase"]}</h4>
                <table class="agg-table"><tbody>{phase_rows}</tbody></table>
            </div>
            <div class="agg-card">
                <h4>{L["countries"]}</h4>
                <table class="agg-table"><tbody>{country_rows}</tbody></table>
            </div>
            <div class="agg-card">
                <h4>{L["registries"]}</h4>
                <table class="agg-table"><tbody>{reg_rows}</tbody></table>
            </div>
            <div class="agg-card">
                <h4>{L["conditions"]}</h4>
                <table class="agg-table"><tbody>{cond_rows}</tbody></table>
            </div>
        </div>

        <!-- Sample trials -->
        <details class="sample-toggle">
            <summary class="sample-summary">{L["sample"]}</summary>
            <div class="card-container" style="margin-top:12px;">
                {sample_cards}
            </div>
        </details>

        <div class="quality-note" style="margin-top:12px;padding:12px;background:#f0fdf4;border-radius:6px;">
            <strong>⚠️ {L["data_note"]}</strong>：{L["data_note_text"]}
        </div>
    </div>
    """
