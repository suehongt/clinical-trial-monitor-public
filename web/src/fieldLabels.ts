/**
 * Human-readable change-field labels (Phase 3E requirement #12).
 *
 * Mirrors server/app.py FIELD_LABELS so change history stays readable in
 * static mode too (where no server is present to label fields).  API
 * payloads carry authoritative `field_label` / `field_label_zh`; this map
 * is the fallback for anything unlabelled.  Raw/internal field names stay
 * available for the advanced details view — never as the primary label.
 */

import type { Lang } from "./i18n";

const LABELS: Record<string, [string, string]> = {
  // field_name: [english, chinese]
  status_id: ["Recruitment Status", "招募状态"],
  enrollment: ["Enrollment", "入组人数"],
  start_date: ["Start Date", "开始日期"],
  primary_completion_date: ["Primary Completion Date", "主要完成日期"],
  completion_date: ["Completion Date", "研究完成日期"],
  registration_date: ["Registration Date", "注册日期"],
  primary_endpoint: ["Primary Outcome", "主要终点指标"],
  secondary_endpoints: ["Secondary Outcomes", "次要终点指标"],
  interventions: ["Interventions", "干预措施"],
  arm_group_interventions: ["Arm Interventions", "分组干预"],
  eligibility_criteria: ["Eligibility Criteria", "入选排除标准"],
  sponsors: ["Sponsor", "申办方"],
  collaborators: ["Collaborators", "合作方"],
  countries: ["Countries", "国家/地区"],
  locations: ["Locations", "研究地点"],
  contacts: ["Contacts", "联系方式"],
  study_design: ["Study Design", "研究设计"],
  study_phase: ["Study Phase", "研究分期"],
  conditions: ["Conditions", "适应症"],
  title: ["Title", "研究题目"],
  scientific_title: ["Scientific Title", "科学题目"],
  last_updated_at_source: ["Last Updated at Source", "注册库更新时间"],
};

/** Best display label for a change field in the UI language. */
export function fieldLabel(
  fieldName: string,
  lang: Lang,
  labelled?: { field_label?: string | null; field_label_zh?: string | null } | null,
): string {
  if (labelled) {
    const server = lang === "zh" ? labelled.field_label_zh : labelled.field_label;
    if (server) return server;
  }
  const pair = LABELS[fieldName];
  if (pair) return lang === "zh" ? pair[1] : pair[0];
  // last resort: humanize the raw path ("primary_completion_date" →
  // "Primary completion date") — readable, and the raw name stays visible
  const pretty = fieldName.replace(/_/g, " ").trim();
  return pretty ? pretty[0].toUpperCase() + pretty.slice(1) : fieldName;
}

/** Field-group labels (the watch taxonomy groups, e.g. "primary_outcomes"). */
const GROUP_LABELS: Record<string, [string, string]> = {
  status: ["Status", "状态"],
  enrollment: ["Enrollment", "入组人数"],
  dates: ["Dates", "日期"],
  primary_outcomes: ["Primary Outcomes", "主要终点"],
  secondary_outcomes: ["Secondary Outcomes", "次要终点"],
  interventions: ["Interventions", "干预措施"],
  arms: ["Arms", "试验分组"],
  eligibility: ["Eligibility", "入选标准"],
  sponsor: ["Sponsor", "申办方"],
  collaborators: ["Collaborators", "合作方"],
  countries: ["Countries", "国家/地区"],
  sites: ["Sites", "研究地点"],
  contacts: ["Contacts", "联系方式"],
  study_design: ["Study Design", "研究设计"],
  other: ["Other", "其他"],
};

export function fieldGroupLabel(group: string, lang: Lang): string {
  const pair = GROUP_LABELS[group];
  if (pair) return lang === "zh" ? pair[1] : pair[0];
  return group.replace(/_/g, " ");
}
