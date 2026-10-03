import type { NotificationRow, ScopedUpdateItem, Severity } from "./types";

export const SEVERITY_ORDER: Record<string, number> = { critical: 0, important: 1, normal: 2, minor: 3 };
export const VALID_SCOPES = ["monitored", "watched", "monitor", "project", "all"] as const;
export const VALID_WINDOWS = ["24h", "7d", "30d", "all"] as const;

export type IntelligenceFilters = {
  scope: typeof VALID_SCOPES[number]; window: typeof VALID_WINDOWS[number]; registry: string;
  severity: "" | Severity | "priority"; category: string; projectId: string; monitorId: string; page: number;
};

export function parseIntelligenceFilters(query: URLSearchParams, allowAllWindow = true): IntelligenceFilters {
  const rawScope = query.get("scope") ?? "monitored";
  const projectId = query.get("project_id") ?? "";
  const monitorId = query.get("monitor_id") ?? query.get("monitor") ?? "";
  const scope = VALID_SCOPES.includes(rawScope as IntelligenceFilters["scope"])
    && (rawScope !== "project" || projectId) && (rawScope !== "monitor" || monitorId)
    ? rawScope as IntelligenceFilters["scope"] : "monitored";
  const allowedWindows = allowAllWindow ? VALID_WINDOWS : VALID_WINDOWS.slice(0, 3);
  const rawWindow = query.get("window") ?? "7d";
  const window = allowedWindows.includes(rawWindow as never) ? rawWindow as IntelligenceFilters["window"] : "7d";
  const rawSeverity = query.get("severity") ?? "";
  const severity = ["critical", "important", "normal", "minor", "priority"].includes(rawSeverity)
    ? rawSeverity as IntelligenceFilters["severity"] : "";
  const page = Math.max(1, Number.parseInt(query.get("page") ?? "1", 10) || 1);
  return { scope, window, registry: query.get("registry") ?? "", severity, category: query.get("category") ?? "", projectId, monitorId, page };
}

export function serializeIntelligenceFilters(filters: IntelligenceFilters): URLSearchParams {
  const q = new URLSearchParams({ scope: filters.scope, window: filters.window });
  if (filters.registry) q.set("registry", filters.registry);
  if (filters.severity) q.set("severity", filters.severity);
  if (filters.category) q.set("category", filters.category);
  if (filters.scope === "project" && filters.projectId) q.set("project_id", filters.projectId);
  if (filters.scope === "monitor" && filters.monitorId) q.set("monitor", filters.monitorId);
  if (filters.page > 1) q.set("page", String(filters.page));
  return q;
}

export const sourceTrialKey = (source: string, id: string) => `${source}:${id}`;
export const dateKey = (stamp: string) => stamp.slice(0, 10) || "unknown";

export type TrialChangeGroup = { key: string; source: string; trialId: string; title: string; items: ScopedUpdateItem[] };
export type DateChangeGroup = { date: string; trials: TrialChangeGroup[] };

export function groupUpdates(items: ScopedUpdateItem[]): DateChangeGroup[] {
  const dates = new Map<string, Map<string, TrialChangeGroup>>();
  for (const item of items) {
    const date = dateKey(item.detected_at);
    const byTrial = dates.get(date) ?? new Map<string, TrialChangeGroup>();
    const key = sourceTrialKey(item.source, item.trial_id);
    const trial = byTrial.get(key) ?? { key, source: item.source, trialId: item.trial_id, title: item.title, items: [] };
    trial.items.push(item); byTrial.set(key, trial); dates.set(date, byTrial);
  }
  return Array.from(dates, ([date, trials]) => ({ date, trials: Array.from(trials.values()) }));
}

export function sortBySeverity<T extends { severity?: string | null }>(items: T[]): T[] {
  return [...items].sort((a, b) => (SEVERITY_ORDER[a.severity ?? "normal"] ?? 2) - (SEVERITY_ORDER[b.severity ?? "normal"] ?? 2));
}

export function sortCategories(categories: Record<string, number>): Array<[string, number]> {
  return Object.entries(categories).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function formatChangeElement(el: unknown): string {
  if (el == null) return "";
  if (typeof el !== "object") return String(el).trim();
  if (Array.isArray(el)) return el.map(formatChangeElement).filter(Boolean).join(", ");
  const obj = el as Record<string, unknown>;
  const name = ["name", "title", "label", "id"].map((k) => obj[k]).find((v) => typeof v === "string" && v.trim());
  if (typeof name === "string") {
    const role = ["role", "type", "status"].map((k) => obj[k]).find((v) => typeof v === "string" && v.trim());
    return typeof role === "string" ? `${name} (${role})` : name;
  }
  return JSON.stringify(obj);
}

export function formatChangeValue(value: unknown, missing: string): string {
  if (value == null || value === "") return missing;
  const join = (arr: unknown[]): string => arr.map(formatChangeElement).filter(Boolean).join(", ") || missing;
  if (Array.isArray(value)) return value.length ? join(value) : missing;
  if (typeof value !== "string") return JSON.stringify(value);
  const text = value.trim();
  if (!text) return missing;
  if (text.startsWith("[") && text.endsWith("]")) {
    try { const parsed: unknown = JSON.parse(text); if (Array.isArray(parsed)) return parsed.length ? join(parsed) : missing; } catch { /* preserve source text */ }
  }
  return text.replace(/^"|"$/g, "");
}

export function summarizeFreshness(sources: Array<{ freshness?: string; state?: string }>): Record<string, number> {
  return sources.reduce<Record<string, number>>((acc, source) => {
    const state = source.freshness ?? source.state ?? "unknown";
    acc[state] = (acc[state] ?? 0) + 1; return acc;
  }, {});
}

export function notificationRows(rows: NotificationRow[], unreadOnly: boolean): NotificationRow[] {
  return unreadOnly ? rows.filter((row) => !row.read_at) : rows;
}

export function updateNotificationRead(rows: NotificationRow[], id: number, read: boolean, now = "now"): NotificationRow[] {
  return rows.map((row) => row.id === id ? { ...row, read_at: read ? now : null } : row);
}

export function clearUpdateFilters(query: URLSearchParams): URLSearchParams {
  const current = parseIntelligenceFilters(query);
  return serializeIntelligenceFilters({ ...current, registry: "", severity: "", category: "", page: 1 });
}

export const toggleCategory = (current: string | null, category: string) => current === category ? null : category;
