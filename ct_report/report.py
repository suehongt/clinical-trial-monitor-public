"""ct_report.report — generate_report(): the cross-source trial HTML report builder."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from ct_report.constants import (PROFILE_KEY, PROFILE_LABEL,
                                 PROFILE_LABEL_EN, SEARCH_KEYWORDS,
                                 SOURCE_COLORS, SOURCE_LABELS, SOURCE_LABELS_EN)
from config import MI_REPORT_KEYWORDS
from ct_report.diffing import _load_reported_map, _record_signature, _trial_key, ensure_registry, get_last_report, save_report_registry
from ct_report.paths import REPORT_DIR
from ct_report.query import extract_from_payload, fetch_cross_source_siblings, get_events_for_record, query_trials
from ct_report.render import (_calc_target_size_total, _eligibility_split, _extract_brief_summary, _extract_ictrp_criteria, _extract_investigator, format_cde_endpoint_table, _render_ictrp_aggregate, _render_quality_overview, format_cross_source_badge, parse_cde_endpoint_table, render_trial_card)
from ct_report.translate import _batch_translate_llm, _load_translation_cache, _translate_zh, _translation_cache, _unwrap_json_list
import logging

logger = logging.getLogger(__name__)


def _report_key(keywords: Optional[List[str]]) -> str:
    """Baseline registry key for the current keyword set.

    The historical MI keyword set keeps the 'mi_cross_source' key so
    existing MI baselines stay valid; any other set (including another
    disease profile's default set) gets a stable derived key so different
    diseases never overwrite each other's baselines.
    """
    ks = keywords or SEARCH_KEYWORDS
    if {k.lower() for k in ks} == {k.lower() for k in MI_REPORT_KEYWORDS}:
        return "mi_cross_source"
    digest = hashlib.sha256("|".join(sorted(k.lower() for k in ks)).encode()).hexdigest()
    return f"cross_source_{digest[:12]}"


def generate_report(
    output: Optional[str] = None,
    force: bool = False,
    keywords: Optional[List[str]] = None,
    since: Optional[str] = None,
    english: bool = False,
    *,
    update_checkpoint: bool = True,
) -> str:
    """生成跨源临床试验HTML报告（疾病口径由 CT_DISEASE_PROFILE 决定，默认 MI）。

    Args:
        output: 输出HTML文件路径，默认 reports/{profile}_report_{date}.html
        force: 忽略增量检查，强制重新生成
        keywords: 搜索关键词列表（默认取当前 profile 的 report_keywords）
        since: 只查询指定时间之后的记录 (ISO format)
        english: 使用英文输出（无翻译、无数据质量说明）
        update_checkpoint: False 时只生成报告、不更新 report_registry 基线
            （one-off / analysis 运行，避免移动增量检查点）

    Returns:
        str: HTML文件路径
    """
    ensure_registry()

    report_key = _report_key(keywords)
    last_report = get_last_report(report_key)
    is_incremental = False
    new_count = 0
    changed_count = 0
    new_ids: List[str] = []
    changed_ids: List[str] = []
    prev_generated_at = None

    if force:
        logger.info("Force regeneration requested; building full report.")
        all_trials = query_trials(keywords=keywords, since=since)
        trials = all_trials
        # 全量重建同样对照上次报告基线做标注（不筛列表）：daily --full
        # 场景下报告仍是全集，但 NEW/CHANGED 卡片与 old→new 块让「这 24
        # 小时内发生了什么」直接可见。
        if last_report and not since:
            prev_map = _load_reported_map(last_report)
            if prev_map:
                prev_generated_at = last_report["generated_at"]
                cur_map = {_trial_key(t): _record_signature(t) for t in all_trials}
                new_ids = [k for k in cur_map if k not in prev_map]
                changed_ids = [k for k in cur_map if k in prev_map
                               and cur_map[k] != prev_map[k]]
                new_count = len(new_ids)
                changed_count = len(changed_ids)
                logger.info(
                    "Force report baseline diff: %d new, %d changed since %s",
                    new_count, changed_count, prev_generated_at,
                )
    elif since:
        # Explicit time-window query → treated as a filtered full report
        all_trials = query_trials(keywords=keywords, since=since)
        trials = all_trials
    elif last_report:
        # Incremental mode: diff the current DB against the last reported set
        prev_generated_at = last_report["generated_at"]
        all_trials = query_trials(keywords=keywords)  # full current set, for diffing
        prev_map = _load_reported_map(last_report)
        if prev_map:
            is_incremental = True
            cur_map = {_trial_key(t): _record_signature(t) for t in all_trials}
            new_ids = [k for k in cur_map if k not in prev_map]
            changed_ids = [k for k in cur_map if k in prev_map and cur_map[k] != prev_map[k]]
            new_count = len(new_ids)
            changed_count = len(changed_ids)
            updated_ids = set(new_ids) | set(changed_ids)
            trials = [t for t in all_trials if _trial_key(t) in updated_ids]
            logger.info(
                "Incremental report: %d new, %d changed since %s",
                new_count, changed_count, prev_generated_at,
            )
        else:
            # Legacy entry with no stored id set → fall back to time-window diff
            logger.info("No baseline id set in registry; using time-window fallback.")
            all_trials = query_trials(keywords=keywords, since=prev_generated_at)
            trials = all_trials
    else:
        all_trials = query_trials(keywords=keywords, since=since)
        trials = all_trials

    if not trials and not force:
        logger.info("No matching trials found. Use --force to regenerate empty reports.")
        # Still generate a (small) report to mark the checkpoint
    elif not trials:
        logger.warning("No matching trials found for keywords: %s", keywords or SEARCH_KEYWORDS)

    # Prepare output path
    if output:
        out_path = Path(output)
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = REPORT_DIR / f"{PROFILE_KEY}_report_{date_str}.html"

    # Group by source
    by_source: Dict[str, List[Dict]] = {}
    for t in trials:
        src = t["short_name"]
        by_source.setdefault(src, []).append(t)

    # Split ICTRP into native ICTRP vs ChiCTR-via-ICTRP
    ictrp_native: List[Dict] = []
    ictrp_chictr: List[Dict] = []
    for t in by_source.get("ICTRP", []):
        sid = t["source_trial_id"]
        if sid.startswith("ChiCTR") or sid.startswith("CHICTR") or sid.startswith("chictr"):
            ictrp_chictr.append(t)
        else:
            ictrp_native.append(t)
    by_source["ICTRP_native"] = ictrp_native
    by_source["ICTRP_ChiCTR"] = ictrp_chictr

    # ── Pre-translate ICTRP_ChiCTR fields via LLM ────────────────────
    # English reports render raw text — skip the expensive translation pass.
    if ictrp_chictr and not english:
        _load_translation_cache()
        translate_texts: list[str] = []
        for t in ictrp_chictr:
            for field in ("title", "scientific_title", "conditions",
                          "study_phase", "primary_endpoint", "secondary_endpoints",
                          "study_type_label", "status_label", "sponsors",
                          "eligibility_criteria"):
                val = t.get(field)
                if val and val != "-":
                    text = _unwrap_json_list(str(val))
                    if text and text not in _translation_cache():
                        translate_texts.append(text)
            # Also pre-translate extracted fields
            raw = t.get("raw_payload") or ""
            for extra_field in ("study_leader", "study_purpose", "brief_title",
                                "official_title", "lead_sponsor_name",
                                "overall_official", "principal_investigator"):
                # Extract the value from raw_payload and add to translate list
                try:
                    rp = json.loads(raw) if isinstance(raw, str) else {}
                except Exception:
                    rp = {}
                ev = rp.get(extra_field) or ""
                if isinstance(ev, str) and ev and ev != "-" and ev not in _translation_cache():
                    translate_texts.append(ev)
            # Also pre-translate inclusion_criteria/exclusion_criteria from ICTRP raw_payload
            try:
                rp = json.loads(raw) if isinstance(raw, str) else {}
                who_rec = rp.get("who_ictrp_record", {})
                for cri_field in ("inclusion_criteria", "exclusion_criteria"):
                    cv = who_rec.get(cri_field, "")
                    if cv and cv not in _translation_cache():
                        translate_texts.append(cv)
            except Exception:
                pass
        if translate_texts:
            logger.info(f"Translating {len(translate_texts)} ICTRP_ChiCTR fields via LLM...")
            _batch_translate_llm(translate_texts)
    else:
        _load_translation_cache()

    # ── Cross-source sibling index (same master trial, other registries) ──
    siblings_map = fetch_cross_source_siblings()

    # Generate HTML
    total = len(trials)
    gen_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Data quality overview ─────────────────────────────────────
    if english:
        html_parts: list[str] = []
    else:
        quality_html = _render_quality_overview(by_source)
        html_parts: list[str] = [quality_html]
    for src_name in ["NCT", "ChiCTR", "CTR", "CTIS", "ISRCTN", "EUCTR", "ICTRP_ChiCTR", "ICTRP_native"]:
        src_trials = by_source.get(src_name, [])
        labels = SOURCE_LABELS_EN if english else SOURCE_LABELS
        src_label = labels.get(src_name, src_name)
        src_color = SOURCE_COLORS.get(src_name, "#6b7280")
        count = len(src_trials)

        # For ICTRP native (large volume), render aggregate stats + sample.
        # In incremental mode, always render individual cards so the delta is complete.
        if src_name == "ICTRP_native" and count > 100 and not is_incremental:
            source_section = _render_ictrp_aggregate(src_trials, src_label, src_color, src_name=src_name, english=english)
        else:
            cards_html = ""
            translate = False if english else (src_name == "ICTRP_ChiCTR")
            new_set = set(new_ids)
            changed_set = set(changed_ids)
            for t in src_trials:
                # For ICTRP records, extract inclusion/exclusion directly from raw_payload
                # (WHO XML has separate <Inclusion_Criteria> and <Exclusion_Criteria> fields)
                ictrp_inc, ictrp_exc = _extract_ictrp_criteria(t.get("raw_payload"))
                if ictrp_inc is not None or ictrp_exc is not None:
                    # Direct extraction succeeded — translate separately if needed
                    inc_raw = ictrp_inc or ""
                    exc_raw = ictrp_exc or ""
                    if translate:
                        if inc_raw:
                            inc_raw = _translate_zh(inc_raw) or inc_raw
                        if exc_raw:
                            exc_raw = _translate_zh(exc_raw) or exc_raw
                    inclusion = inc_raw[:2000] if inc_raw else "-"
                    exclusion = exc_raw[:2000] if exc_raw else "-"
                else:
                    # Fall back to combined eligibility_criteria + split
                    ec = t["eligibility_criteria"]
                    if translate and ec and ec != "-":
                        ec = _translate_zh(ec) or ec
                    inclusion, exclusion = _eligibility_split(ec)

                # Extract PI and study purpose from raw_payload if available
                extra = extract_from_payload(
                    t.get("raw_payload"),
                    "study_leader",
                    "study_purpose",
                    "brief_title",
                    "official_title",
                    "lead_sponsor_name",
                    "overall_official",
                    "principal_investigator",
                )
                pi = extra.get("study_leader") or extra.get("overall_official") or extra.get("principal_investigator") or _extract_investigator(t.get("raw_payload")) or "-"
                purpose = extra.get("study_purpose") or _extract_brief_summary(t.get("raw_payload")) or "-"

                # CTR endpoint fields are flattened CDE tables — rebuild
                # the table and render it as HTML instead of cell soup.
                if t["short_name"] == "CTR":
                    cde_primary_html = format_cde_endpoint_table(
                        parse_cde_endpoint_table(t.get("primary_endpoint")))
                    sec_cells = t.get("secondary_endpoints")
                    try:
                        sec_items = json.loads(sec_cells) if sec_cells and sec_cells.strip().startswith("[") else None
                    except json.JSONDecodeError:
                        sec_items = None
                    sec_text = "\n".join(str(x) for x in sec_items) if isinstance(sec_items, list) else sec_cells
                    cde_secondary_html = format_cde_endpoint_table(
                        parse_cde_endpoint_table(sec_text))
                else:
                    cde_primary_html = cde_secondary_html = None

                # Calculate enrollment: prefer group-summary from target_size
                enrollment_display = _calc_target_size_total(t.get("raw_payload")) if translate else None
                if not enrollment_display:
                    enrollment_display = str(t.get("enrollment") or "-")

                study_type = t.get("study_type_label") or "-"
                status = t.get("status_label") or "-"

                sibling_chips = format_cross_source_badge(siblings_map.get(_trial_key(t)), english=english)
                t_key = _trial_key(t)
                t_is_new = t_key in new_set
                t_is_changed = t_key in changed_set
                t_change_events = (get_events_for_record(t["record_id"])
                                   if (t_is_new or t_is_changed) else [])
                cards_html += render_trial_card(
                    t,
                    status=status,
                    study_type=study_type,
                    enrollment_display=enrollment_display,
                    pi=pi,
                    purpose=purpose,
                    inclusion=inclusion,
                    exclusion=exclusion,
                    sibling_chips_html=sibling_chips,
                    cde_primary_html=cde_primary_html,
                    cde_secondary_html=cde_secondary_html,
                    is_new=t_is_new,
                    is_changed=t_is_changed,
                    change_events=t_change_events,
                    translate=translate,
                    english=english,
                )

            empty_label = "No matching trials" if english else "该数据源暂无匹配结果"
            count_label = f"{count} {'trials' if english else '项试验'}"
            source_section = f"""
            <div class="source-section" data-source="{src_name}">
                <div class="source-header" style="border-left: 4px solid {src_color};">
                    <h2>{src_label}</h2>
                    <span class="count-badge">{count_label}</span>
                </div>
                <div class="card-container">
                    {cards_html if cards_html else f'<div class="empty-state">{empty_label}</div>'}
                </div>
            </div>
            """

        html_parts.append(source_section)

    src_label_suffix = " trials" if english else " 项"
    sources_summary = "".join(
        f'<div class="summary-source"><span class="dot" style="background:{SOURCE_COLORS.get(s, "#6b7280")}"></span>{labels.get(s, s)}: {len(by_source.get(s, []))}{src_label_suffix}</div>'
        for s in ["NCT", "ChiCTR", "CTR", "CTIS", "ISRCTN", "EUCTR", "ICTRP_native", "ICTRP_ChiCTR"]
    )
    n_chictr_direct = len(by_source.get("ChiCTR", []))
    n_chictr_ictrp = len(by_source.get("ICTRP_ChiCTR", []))

    html_lang = "en" if english else "zh-CN"
    report_title = (f"{PROFILE_LABEL_EN} Clinical Trials — Cross-Source Report"
                    if english else f"{PROFILE_LABEL}临床试验跨源综合报告")
    header_subtitle = "Data Sources: ClinicalTrials.gov · ChiCTR · ChinaDrugTrials · EU CTIS · ISRCTN · EUCTR · WHO ICTRP" if english else "数据来源：ClinicalTrials.gov · 中国临床试验注册中心 · 中国药物临床试验登记平台 · 欧盟 CTIS · ISRCTN · EUCTR 历史库 · WHO ICTRP"
    gen_time_label = "Generated: " if english else "生成时间："
    total_label = "Total Trials" if english else "总试验数"
    chictr_label = "ChiCTR Total" if english else "ChiCTR 合计"
    footer_text = "Generated by Global Clinical Trial Monitor" if english else "报告由 Global Clinical Trial Monitor 自动生成 · 数据延迟取决于各数据源更新频率"

    # Incremental (delta) mode: relabel the report so it is obviously NOT the full set
    if is_incremental:
        mode_tag = " (Incremental)" if english else "（增量更新）"
        report_title = (f"{PROFILE_LABEL_EN} Clinical Trials — Cross-Source Update Report"
                        if english else f"{PROFILE_LABEL}临床试验跨源更新报告") + mode_tag
        total_label = "Updates" if english else "更新项"
        update_note = (
            f"Since {prev_generated_at}: {new_count} new, {changed_count} changed. "
            f"Full report via --force."
            if english else
            f"自 {prev_generated_at} 以来：新增 {new_count} 项 · 变更 {changed_count} 项。"
            f"完整报表请用 --force 生成。"
        )
    elif new_count or changed_count:
        # Full rebuild with baseline annotation — full set on display, the
        # delta since the previous report marked on the cards themselves.
        update_note = (
            f"Since previous report ({prev_generated_at}): {new_count} new, "
            f"{changed_count} changed (marked on cards)."
            if english else
            f"较上次报告（{prev_generated_at}）：新增 {new_count} 项 · "
            f"变更 {changed_count} 项（已在卡片上标注）。"
        )
    else:
        update_note = ""

    html = f"""<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{report_title}</title>
<style>
  :root {{
    --bg: #f8fafc;
    --card-bg: #ffffff;
    --text: #1e293b;
    --text-secondary: #64748b;
    --border: #e2e8f0;
    --accent: #2563eb;
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 0;
  }}

  .container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px;
  }}

  /* Header */
  .report-header {{
    background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
    color: white;
    padding: 48px 24px;
    text-align: center;
  }}

  .report-header h1 {{
    font-size: 28px;
    font-weight: 700;
    margin-bottom: 8px;
  }}

  .report-header .subtitle {{
    color: #94a3b8;
    font-size: 14px;
  }}

  /* Summary bar */
  .summary-bar {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    justify-content: center;
    padding: 20px 24px;
    background: var(--card-bg);
    border-bottom: 1px solid var(--border);
  }}

  .summary-item {{
    text-align: center;
    padding: 0 16px;
  }}

  .summary-item .num {{
    font-size: 32px;
    font-weight: 700;
    color: var(--accent);
  }}

  .summary-item .label {{
    font-size: 12px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  .summary-source {{
    font-size: 13px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 2px 0;
  }}

  .incremental-banner {{
    background: #eff6ff;
    border-bottom: 1px solid #bfdbfe;
    color: #1e40af;
    padding: 12px 24px;
    font-size: 14px;
    text-align: center;
    font-weight: 500;
  }}

  .dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
  }}

  /* Source section */
  .source-section {{
    margin-bottom: 32px;
  }}

  .source-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    background: var(--card-bg);
    border-radius: 8px;
    margin-bottom: 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  }}

  .source-header h2 {{
    font-size: 18px;
    font-weight: 600;
  }}

  .count-badge {{
    background: #f1f5f9;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 13px;
    color: var(--text-secondary);
    font-weight: 500;
  }}

  /* Trial card */
  .card-container {{
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}

  .trial-card {{
    background: var(--card-bg);
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    transition: box-shadow 0.2s;
  }}

  .trial-card:hover {{
    box-shadow: 0 4px 12px rgba(0,0,0,0.12);
  }}

  .card-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 20px;
    background: #f8fafc;
    border-bottom: 1px solid var(--border);
  }}

  .trial-id {{
    font-family: "SF Mono", "Fira Code", monospace;
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
  }}

  .card-body {{
    padding: 20px;
  }}

  .trial-title {{
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 16px;
    color: var(--text);
  }}

  .xsrc-line {{
    font-size: 12px;
    color: var(--text-secondary);
    margin: -10px 0 14px;
  }}
  .xsrc-chip {{
    display: inline-block;
    padding: 1px 8px;
    margin-left: 5px;
    border-radius: 10px;
    background: #eef2ff;
    color: #4f46e5;
    text-decoration: none;
    font-size: 12px;
  }}
  .xsrc-chip:hover {{ background: #e0e7ff; }}

  .field-table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 16px;
  }}

  .field-table td {{
    padding: 6px 8px;
    font-size: 13px;
    vertical-align: top;
  }}

  .field-label {{
    font-weight: 600;
    color: var(--text-secondary);
    white-space: nowrap;
    width: 80px;
  }}

  .na {{
    color: #94a3b8;
    font-style: italic;
  }}

  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    color: white;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }}

  .badge-unknown {{
    background: #94a3b8;
  }}

  .source-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    color: white;
  }}

  /* NEW/CHANGED baseline-diff chips (incremental & force reports) */
  .chip {{
    display: inline-block;
    padding: 2px 8px;
    margin-left: 5px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
  }}
  .chip-new {{
    background: #dcfce7;
    color: #15803d;
  }}
  .chip-changed {{
    background: #fef3c7;
    color: #b45309;
  }}

  /* Field-level old→new change block */
  .change-block {{
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-radius: 6px;
    padding: 8px 12px;
    margin: -6px 0 14px;
  }}
  .change-block-title {{
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #b45309;
    margin-bottom: 4px;
  }}
  .change-row {{
    display: flex;
    gap: 8px;
    font-size: 13px;
    padding: 2px 0;
    align-items: baseline;
  }}
  .change-label {{
    font-weight: 600;
    color: var(--text-secondary);
    white-space: nowrap;
    min-width: 72px;
  }}
  .change-old {{
    color: #9f1239;
    text-decoration: line-through;
    text-decoration-color: #fda4af;
  }}
  .change-arrow {{
    color: #b45309;
    font-weight: 700;
  }}
  .change-new {{
    color: #15803d;
    font-weight: 600;
    word-break: break-all;
  }}

  /* Criteria */
  .criteria-section {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 12px;
  }}

  .criteria-box {{
    background: #f8fafc;
    border-radius: 6px;
    padding: 12px;
  }}

  .criteria-label {{
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }}

  .criteria-label.inclusion {{ color: #16a34a; }}
  .criteria-label.exclusion {{ color: #dc2626; }}

  .criteria-text {{
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-secondary);
    max-height: 120px;
    overflow-y: auto;
  }}

  .criteria-text::-webkit-scrollbar {{
    width: 4px;
  }}

  .criteria-text::-webkit-scrollbar-thumb {{
    background: #cbd5e1;
    border-radius: 2px;
  }}

  .criteria-list {{
    margin: 0;
    padding-left: 18px;
    max-height: 200px;
    overflow-y: auto;
  }}

  .criteria-list li {{
    font-size: 12px;
    line-height: 1.6;
    color: var(--text-secondary);
    margin-bottom: 4px;
    padding-right: 4px;
  }}

  .criteria-list li:last-child {{
    margin-bottom: 0;
  }}

  .criteria-list::-webkit-scrollbar {{
    width: 4px;
  }}

  .criteria-list::-webkit-scrollbar-thumb {{
    background: #cbd5e1;
    border-radius: 2px;
  }}

  /* Footer */
  .card-footer {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    font-size: 12px;
  }}

  .card-footer ul {{
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }}

  .card-footer ul li {{
    background: #f1f5f9;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
  }}

  .sponsor-role {{
    font-size: 10px;
    color: #94a3b8;
    font-weight: 400;
  }}

  .purpose-text {{
    max-height: 80px;
    overflow-y: auto;
    line-height: 1.5;
    font-size: 13px;
  }}

  .purpose-text::-webkit-scrollbar {{
    width: 4px;
  }}

  .purpose-text::-webkit-scrollbar-thumb {{
    background: #cbd5e1;
    border-radius: 2px;
  }}

  .conditions-tags ul {{
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: 0;
    padding: 0;
  }}

  .conditions-tags ul li {{
    background: #eef2ff;
    color: #4338ca;
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
  }}

  .cde-endpoint-table {{
    width: 100%; border-collapse: collapse; font-size: 12.5px;
    border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;
  }}
  .cde-endpoint-table th, .cde-endpoint-table td {{
    padding: 7px 10px; text-align: left; vertical-align: top;
    border-bottom: 1px solid #e2e8f0; line-height: 1.55;
  }}
  .cde-endpoint-table th {{
    background: #f1f5f9; font-weight: 650; color: #475569; font-size: 12px;
    white-space: nowrap;
  }}
  .cde-endpoint-table td.no, .cde-endpoint-table th:first-child {{
    width: 36px; font-family: ui-monospace, Menlo, monospace; color: #64748b;
  }}
  .cde-endpoint-table tr:last-child td {{ border-bottom: none; }}
  .endpoint-list {{
    margin: 0;
    padding-left: 18px;
    list-style: disc;
  }}

  .endpoint-list li {{
    font-size: 12px;
    line-height: 1.6;
    color: var(--text-secondary);
    margin-bottom: 2px;
  }}

  .endpoint-list li:last-child {{
    margin-bottom: 0;
  }}

  .source-link {{
    color: var(--accent);
    text-decoration: none;
    font-weight: 500;
    white-space: nowrap;
  }}

  .source-link:hover {{
    text-decoration: underline;
  }}

  /* Empty state */
  .empty-state {{
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
    font-size: 14px;
  }}

  /* Report footer */
  .report-footer {{
    text-align: center;
    padding: 24px;
    color: var(--text-secondary);
    font-size: 12px;
    border-top: 1px solid var(--border);
    margin-top: 32px;
  }}

  /* Quality cards */
  .quality-card {{
    background: var(--card-bg);
    border-radius: 8px;
    padding: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  .quality-card h3 {{
    font-size: 16px;
    font-weight: 600;
    margin: 16px 0 8px;
  }}
  .quality-card h3:first-child {{ margin-top: 0; }}
  .quality-card ul {{
    padding-left: 20px;
    margin: 4px 0;
  }}
  .quality-card ul li {{
    font-size: 13px;
    line-height: 1.7;
    color: var(--text-secondary);
  }}
  .quality-note {{
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.6;
  }}

  /* Quality matrix table */
  .quality-matrix {{
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 13px;
  }}
  .quality-matrix th,
  .quality-matrix td {{
    padding: 6px 10px;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }}
  .quality-matrix th {{
    background: #f1f5f9;
    font-weight: 600;
    font-size: 12px;
  }}
  .fq-field {{ font-weight: 600; white-space: nowrap; }}
  .fq-cell {{ font-size: 12px; text-align: center; }}

  /* Quality grid */
  .quality-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
    margin-top: 8px;
  }}
  .quality-item {{
    background: #f8fafc;
    border-radius: 6px;
    padding: 12px;
  }}
  .quality-item-header {{
    font-weight: 600;
    font-size: 14px;
    margin-bottom: 6px;
  }}
  .quality-item ul {{ margin: 0; padding-left: 16px; }}
  .quality-item ul li {{ font-size: 12px; line-height: 1.6; }}

  /* Aggregate grid */
  .aggregate-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }}
  .agg-card {{
    background: var(--card-bg);
    border-radius: 8px;
    padding: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  .agg-card h4 {{
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 8px;
  }}
  .agg-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .agg-table td {{
    padding: 4px 6px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }}
  .agg-table tr:last-child td {{ border-bottom: none; }}
  .agg-table td:first-child {{ font-weight: 500; }}
  .agg-table td:nth-child(2) {{ text-align: right; color: var(--accent); font-weight: 600; }}
  .agg-table td:nth-child(3) {{ color: var(--text-secondary); font-size: 11px; }}

  .bar {{
    height: 14px;
    background: linear-gradient(90deg, #3b82f6, #60a5fa);
    border-radius: 3px;
    min-width: 8px;
  }}

  /* Sample toggle */
  .sample-toggle {{
    background: var(--card-bg);
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  .sample-summary {{
    padding: 12px 20px;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    color: var(--accent);
  }}

  /* Search bar */
  .search-bar {{
    max-width: 1200px;
    margin: 16px auto 0;
    padding: 0 24px;
  }}
  .search-input {{
    width: 100%;
    padding: 12px 16px;
    border: 2px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
    background: var(--card-bg);
    color: var(--text);
  }}
  .search-input:focus {{
    border-color: var(--accent);
  }}
  .search-input::placeholder {{
    color: #94a3b8;
  }}
  .search-status {{
    font-size: 12px;
    color: var(--text-secondary);
    padding: 4px 0 8px;
  }}
  .trial-card.hidden {{
    display: none;
  }}

  /* Back to top */
  .back-to-top {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: var(--accent);
    color: white;
    border: none;
    cursor: pointer;
    font-size: 20px;
    box-shadow: 0 2px 8px rgba(37, 99, 235, 0.3);
    opacity: 0;
    visibility: hidden;
    transition: opacity 0.3s, visibility 0.3s;
    z-index: 999;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .back-to-top.visible {{
    opacity: 1;
    visibility: visible;
  }}
  .back-to-top:hover {{
    background: #1d4ed8;
  }}

  /* Source filter chips */
  .filter-bar {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 8px 0 4px;
  }}
  .filter-chip {{
    padding: 6px 16px;
    border-radius: 20px;
    border: 2px solid var(--border);
    background: var(--card-bg);
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }}
  .filter-chip:hover {{
    border-color: var(--accent);
    color: var(--accent);
  }}
  .filter-chip.active {{
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }}

  .source-section.hidden {{
    display: none;
  }}

  /* Responsive */
  @media (max-width: 768px) {{
    .criteria-section {{
      grid-template-columns: 1fr;
    }}
    .summary-bar {{
      flex-direction: column;
      align-items: center;
    }}
    .aggregate-grid {{
      grid-template-columns: 1fr;
    }}
    .quality-grid {{
      grid-template-columns: 1fr;
    }}
  }}
</style>
</head>
<body>

<div class="report-header">
  <h1>{report_title}</h1>
  <div class="subtitle">
    {header_subtitle}
    <br>{gen_time_label}{gen_time}
  </div>
</div>

<div class="summary-bar">
  <div class="summary-item">
    <div class="num">{total}</div>
    <div class="label">{total_label}</div>
  </div>
  <div class="summary-item">
    <div class="num">{len(by_source.get("NCT", []))}</div>
    <div class="label">NCT</div>
  </div>
  <div class="summary-item">
    <div class="num">{n_chictr_direct + n_chictr_ictrp}</div>
    <div class="label">{chictr_label}</div>
  </div>
  <div class="summary-item">
    <div class="num">{len(by_source.get("CTR", []))}</div>
    <div class="label">CTR</div>
  </div>
  <div class="summary-item">
    <div class="num">{len(by_source.get("ICTRP_native", []))}</div>
    <div class="label">ICTRP</div>
  </div>
  <div style="border-left:1px solid var(--border);padding-left:16px;">
    {sources_summary}
  </div>
</div>

{('<div class="incremental-banner">' + update_note + '</div>') if update_note else ''}

<div class="search-bar">
  <input class="search-input" type="text" id="searchInput" placeholder="Search trials by ID, title, conditions..." oninput="filterTrials()">
  <div class="filter-bar">
    <span class="filter-chip active" data-filter="all" onclick="setFilter('all')">All</span>
    <span class="filter-chip" data-filter="NCT" onclick="setFilter('NCT')" style="border-color:#2563eb;color:#2563eb;">ClinicalTrials.gov</span>
    <span class="filter-chip" data-filter="ChiCTR" onclick="setFilter('ChiCTR')" style="border-color:#9333ea;color:#9333ea;">ChiCTR</span>
    <span class="filter-chip" data-filter="CTR" onclick="setFilter('CTR')" style="border-color:#0891b2;color:#0891b2;">CTR</span>
    <span class="filter-chip" data-filter="ICTRP_native" onclick="setFilter('ICTRP_native')" style="border-color:#16a34a;color:#16a34a;">ICTRP (WHO)</span>
    <span class="filter-chip" data-filter="ICTRP_ChiCTR" onclick="setFilter('ICTRP_ChiCTR')" style="border-color:#d97706;color:#d97706;">ICTRP → ChiCTR</span>
  </div>
  <div class="search-status" id="searchStatus"></div>
</div>

<div class="container">
  {''.join(html_parts)}
</div>

<div class="report-footer">
  {footer_text}
</div>

<button class="back-to-top" id="backToTop" onclick="window.scrollTo({{top:0,behavior:'smooth'}})" title="Back to top">&#8593;</button>

<script>
var activeFilter = 'all';

function setFilter(source) {{
  activeFilter = source;
  // Update chip styles
  var chips = document.querySelectorAll('.filter-chip');
  for (var i = 0; i < chips.length; i++) {{
    chips[i].classList.remove('active');
  }}
  document.querySelector('.filter-chip[data-filter=\"' + source + '\"]').classList.add('active');
  applyFilters();
}}

function applyFilters() {{
  var input = document.getElementById('searchInput');
  var status = document.getElementById('searchStatus');
  var textFilter = input.value.toLowerCase();

  // Filter sections by source
  var sections = document.querySelectorAll('.source-section');
  for (var i = 0; i < sections.length; i++) {{
    var sec = sections[i];
    var secSource = sec.getAttribute('data-source');
    if (activeFilter !== 'all' && secSource !== activeFilter) {{
      sec.classList.add('hidden');
    }} else {{
      sec.classList.remove('hidden');
    }}
  }}

  // Filter cards by text within visible sections
  var allCards = document.querySelectorAll('.trial-card');
  var totalVisible = 0;
  for (var i = 0; i < allCards.length; i++) {{
    var card = allCards[i];
    var section = card.closest('.source-section');
    if (section && section.classList.contains('hidden')) {{
      card.classList.add('hidden');
      continue;
    }}
    var text = card.textContent.toLowerCase();
    if (text.indexOf(textFilter) > -1) {{
      card.classList.remove('hidden');
      totalVisible++;
    }} else {{
      card.classList.add('hidden');
    }}
  }}

  // Show/hide sections if all cards hidden
  for (var i = 0; i < sections.length; i++) {{
    if (sections[i].classList.contains('hidden')) continue;
    var cards = sections[i].querySelectorAll('.trial-card');
    var has = false;
    for (var j = 0; j < cards.length; j++) {{
      if (!cards[j].classList.contains('hidden')) {{ has = true; break; }}
    }}
    if (cards.length > 0 && !has) {{
      sections[i].classList.add('hidden');
    }}
  }}

  if (textFilter.length > 0) {{
    status.textContent = totalVisible + ' / ' + allCards.length + ' trials visible';
  }} else {{
    status.textContent = '';
  }}
}}

function filterTrials() {{
  applyFilters();
}}

window.onscroll = function() {{
  var btn = document.getElementById('backToTop');
  if (document.body.scrollTop > 400 || document.documentElement.scrollTop > 400) {{
    btn.classList.add('visible');
  }} else {{
    btn.classList.remove('visible');
  }}
}};
</script>

</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("Report written to %s", out_path)

    # Update registry — store the FULL current set (id + signature) so the next
    # incremental run can compute a true new/changed diff, regardless of what was
    # actually rendered in this (possibly incremental) report.
    # Skipped for one-off / analysis runs (update_checkpoint=False) so the
    # incremental baseline (report_file pointer + generated_at + trial id set)
    # is left untouched.
    if update_checkpoint:
        full_id_sig = [[_trial_key(t), _record_signature(t)] for t in all_trials]
        trial_ids_json = json.dumps(full_id_sig, ensure_ascii=False)
        trial_ids = [t["source_trial_id"] for t in all_trials]
        save_report_registry(
            report_key,
            str(out_path),
            trial_ids,
            {"keywords": keywords or SEARCH_KEYWORDS, "total": len(all_trials),
             "incremental": is_incremental, "new": new_count, "changed": changed_count},
            trial_ids_json=trial_ids_json,
        )
    else:
        logger.info("Checkpoint not updated (update_checkpoint=False).")

    return str(out_path)
