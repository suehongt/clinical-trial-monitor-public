import type { MonitorSummary } from "./types";
import type { ProjectListItem } from "./projectApi";

export type MonitorStatusFilter = "all" | "enabled" | "paused";
export type MonitorScheduleFilter = "all" | "scheduled" | "manual";
export const monitorStatusFilter = (value: string | null): MonitorStatusFilter => value === "enabled" || value === "paused" ? value : "all";
export const monitorScheduleFilter = (value: string | null): MonitorScheduleFilter => value === "scheduled" || value === "manual" ? value : "all";
export function filterMonitors(rows: MonitorSummary[], query: string, status: MonitorStatusFilter, schedule: MonitorScheduleFilter) {
  const q = query.trim().toLocaleLowerCase();
  return rows.filter((row) => (!q || row.name.toLocaleLowerCase().includes(q))
    && (status === "all" || (status === "enabled") === Boolean(Number(row.enabled)))
    && (schedule === "all" || (schedule === "scheduled") === Boolean(Number(row.schedule_enabled))));
}
export type Rules = Record<string, string | string[]>;
export const RULE_FIELDS = ["query", "condition", "intervention", "sponsor", "registries", "statuses", "study_types", "phase", "country"] as const;
export const ruleToText = (value: string | string[] | undefined) => Array.isArray(value) ? value.join(", ") : value ?? "";
export function draftToRule(draft: Rules): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const key of RULE_FIELDS) {
    const terms = ruleToText(draft[key]).split(",").map((value) => value.trim()).filter(Boolean);
    if (terms.length) out[key] = Array.from(new Set(terms));
  }
  return out;
}
export type ProjectView = "active" | "archived";
export const projectView = (value: string | null, legacy?: string | null): ProjectView => value === "archived" || (value == null && legacy === "true") ? "archived" : "active";
export const stableProjectSort = (rows: ProjectListItem[]) => rows.map((row, index) => ({ row, index }))
  .sort((a, b) => Number(b.row.pinned) - Number(a.row.pinned) || Number(Boolean(a.row.archived_at)) - Number(Boolean(b.row.archived_at)) || a.index - b.index).map(({ row }) => row);
export type ProjectTab = "overview" | "trials" | "activity" | "intelligence" | "notebook";
export function projectTab(value: string | null): ProjectTab {
  if (value === "searches" || value === "monitors") return "overview";
  if (value === "updates") return "activity";
  if (value === "briefing") return "intelligence";
  return ["overview", "trials", "activity", "intelligence", "notebook"].includes(value ?? "") ? value as ProjectTab : "overview";
}
export type TrialView = "curated" | "matches" | "watched";
export const trialView = (value: string | null): TrialView => ["matches", "watched"].includes(value ?? "") ? value as TrialView : "curated";
export const positivePage = (value: string | null) => Math.max(1, Number.parseInt(value ?? "1", 10) || 1);
export const sourceTrialKey = (source: string, id: string) => `${source}:${id}`;
export const retryEligible = (status: string) => status === "failed" || status === "interrupted";
export function freshnessSummary(rows: Array<{ enabled: boolean; freshness: string; latest_run_status?: string | null }>) {
  return { enabled: rows.filter((row) => row.enabled).length, fresh: rows.filter((row) => row.freshness === "fresh").length,
    attention: rows.filter((row) => row.freshness === "delayed" || row.freshness === "stale").length,
    failed: rows.filter((row) => retryEligible(row.latest_run_status ?? "")).length };
}
