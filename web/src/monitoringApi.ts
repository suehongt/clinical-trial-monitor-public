/**
 * Typed client for the monitoring API surface (Phase 3E pages).
 *
 * Responses are validated with light structural guards (same philosophy as
 * guards.ts — reject non-conforming payloads before they reach the UI).
 */
import { fetchJson, mutateJson } from "./api";
import type {
  DashboardData, LiveCheckResponse, MonitorActivityRow, MonitorRunRow,
  MonitorSummary, MonitorTrialRow, NotificationRow, ScopedUpdatesData,
  TimelineGroup, TrialDetailFull, TrialEvent, TrialVersion, TrialWatch,
  VersionDifference, WatchedTrial,
} from "./types";

const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const list = <T,>(key: string) => (v: unknown): v is { generated_at: string } & Record<string, T[]> =>
  object(v) && Array.isArray(v[key]);
const enc = encodeURIComponent;

/* ── watches ──────────────────────────────────────────────────────────── */
/** Payload guard for GET /api/watches, shared by the cached read paths. */
export const isWatchList = (v: unknown): v is { generated_at: string; watches: WatchedTrial[] } =>
  object(v) && Array.isArray((v as { watches?: unknown }).watches);
export const getWatches = () =>
  fetchJson("/api/watches", list<WatchedTrial>("watches") as unknown as (v: unknown) => v is { generated_at: string; watches: WatchedTrial[] });
export const watchTrial = (source: string, id: string) =>
  mutateJson<{ generated_at: string; watch: TrialWatch }>(`/api/trials/${enc(source)}/${enc(id)}/watch`, "POST", undefined, object as (v: unknown) => v is { generated_at: string; watch: TrialWatch });
export const unwatchTrial = (source: string, id: string) =>
  mutateJson<{ generated_at: string; watch: TrialWatch | null }>(`/api/trials/${enc(source)}/${enc(id)}/watch`, "DELETE", undefined, object as (v: unknown) => v is { generated_at: string; watch: TrialWatch | null });
export const updatePreferences = (watchId: number, preferences: TrialWatch["preferences"]) =>
  mutateJson<{ watch: TrialWatch }>(`/api/watches/${watchId}/preferences`, "PATCH", preferences, object as (v: unknown) => v is { watch: TrialWatch });

/* ── trial change history / timeline / versions ───────────────────────── */
export const isTrialChanges = (v: unknown): v is { total: number; events: TrialEvent[] } =>
  object(v) && Array.isArray((v as { events?: unknown }).events);
export const isTrialVersions = (v: unknown): v is { versions: TrialVersion[] } =>
  object(v) && Array.isArray((v as { versions?: unknown }).versions);
export const isTrialMirrors = (v: unknown): v is { total: number; siblings: NonNullable<TrialDetailFull["siblings"]> } =>
  object(v) && Array.isArray((v as { siblings?: unknown }).siblings);
export const getTrialChanges = (source: string, id: string, severity = "") =>
  fetchJson<{ total: number; events: TrialEvent[] }>(
    `/api/trials/${enc(source)}/${enc(id)}/changes?limit=500${severity ? `&severity=${enc(severity)}` : ""}`,
    list<TrialEvent>("events") as unknown as (v: unknown) => v is { total: number; events: TrialEvent[] });
export const getTimeline = (source: string, id: string) =>
  fetchJson<{ timeline: TimelineGroup[] }>(`/api/trials/${enc(source)}/${enc(id)}/timeline`, list<TimelineGroup>("timeline") as unknown as (v: unknown) => v is { timeline: TimelineGroup[] });
export const getVersions = (source: string, id: string) =>
  fetchJson<{ versions: TrialVersion[] }>(`/api/trials/${enc(source)}/${enc(id)}/versions`, list<TrialVersion>("versions") as unknown as (v: unknown) => v is { versions: TrialVersion[] });
export const compareVersions = (source: string, id: string, from: number, to: number) =>
  fetchJson<{ changes: VersionDifference[] }>(`/api/trials/${enc(source)}/${enc(id)}/versions/${from}/compare/${to}`, list<VersionDifference>("changes") as unknown as (v: unknown) => v is { changes: VersionDifference[] });
export const getTrialMirrors = (source: string, id: string) =>
  fetchJson<{ total: number; siblings: NonNullable<TrialDetailFull["siblings"]> }>(
    `/api/trials/${enc(source)}/${enc(id)}/mirrors`,
    (v): v is { total: number; siblings: NonNullable<TrialDetailFull["siblings"]> } =>
      object(v) && Array.isArray((v as { siblings?: unknown }).siblings));

/* ── dashboard aggregate ──────────────────────────────────────────────── */
export const getDashboard = (window: "24h" | "7d" | "30d") =>
  fetchJson<DashboardData>(`/api/dashboard?window=${window}`, (v): v is DashboardData => {
    if (!object(v)) return false;
    const d = v as unknown as DashboardData;
    return typeof d.summary === "object" && d.summary !== null
      && Array.isArray(d.updates) && Array.isArray(d.monitors) && Array.isArray(d.watched_activity);
  });

export const getScopedUpdates = (params: URLSearchParams) =>
  fetchJson<ScopedUpdatesData>(`/api/updates?${params.toString()}`,
    (v): v is ScopedUpdatesData => object(v) && Array.isArray((v as { items?: unknown }).items)
      && typeof (v as { total?: unknown }).total === "number");
/** Payload guard for GET /api/updates?…, shared by the cached read path. */
export const isScopedUpdates = (v: unknown): v is ScopedUpdatesData =>
  object(v) && Array.isArray((v as { items?: unknown }).items)
  && typeof (v as { total?: unknown }).total === "number";

/* ── trial detail page (light shape + memberships + mirrors) ──────────── */
export const getTrialFull = (source: string, id: string) =>
  fetchJson<TrialDetailFull & Record<string, unknown>>(
    `/api/trials/${enc(source)}/${enc(id)}`,
    (v): v is TrialDetailFull & Record<string, unknown> => object(v));

/* ── live source check (ChiCTR/CTR, WAF-cooled) ───────────────────────── */
export const liveCheckTrials = (
  q: string,
  opts?: { sources?: string[]; max_pages?: number; enrich_limit?: number;
           continue?: boolean; full_ingest?: boolean },
) =>
  mutateJson<LiveCheckResponse>("/api/trials/live-check", "POST", {
    q,
    sources: opts?.sources ?? ["chictr", "ctr", "nct", "ictrp"],
    max_pages: opts?.max_pages ?? 5,
    enrich_limit: opts?.enrich_limit ?? 6,
    ...(opts?.continue ? { continue: true } : {}),
    ...(opts?.full_ingest ? { full_ingest: true } : {}),
  }, (v): v is LiveCheckResponse =>
    object(v) && typeof (v as { q?: unknown }).q === "string"
      && Array.isArray((v as { results?: unknown }).results));

/* ── monitors ─────────────────────────────────────────────────────────── */
const isMonitorSummary = (v: unknown): v is MonitorSummary =>
  object(v) && typeof (v as { id?: unknown }).id === "number";
/** Payload guards for the monitor GETs, shared by the cached read paths. */
export const isMonitorListPayload = (v: unknown): v is { monitors: MonitorSummary[] } =>
  object(v) && Array.isArray((v as { monitors?: unknown }).monitors);
export const isMonitorPayload = (v: unknown): v is { monitor: MonitorSummary } =>
  object(v) && isMonitorSummary((v as { monitor?: unknown }).monitor);
export const isMonitorTrialsPayload = (v: unknown): v is { trials: MonitorTrialRow[] } =>
  object(v) && Array.isArray((v as { trials?: unknown }).trials);
export const isMonitorActivityPayload = (v: unknown): v is { activity: MonitorActivityRow[] } =>
  object(v) && Array.isArray((v as { activity?: unknown }).activity);
export const isMonitorRunsPayload = (v: unknown): v is { runs: MonitorRunRow[] } =>
  object(v) && Array.isArray((v as { runs?: unknown }).runs);
export const getMonitors = () =>
  fetchJson<{ monitors: MonitorSummary[] }>("/api/monitors",
    (v): v is { monitors: MonitorSummary[] } => object(v) && Array.isArray((v as { monitors?: unknown }).monitors));
export const getMonitor = (id: number) =>
  fetchJson<{ monitor: MonitorSummary }>(`/api/monitors/${id}`,
    (v): v is { monitor: MonitorSummary } => object(v) && isMonitorSummary((v as { monitor?: unknown }).monitor));
export const createMonitor = (payload: Record<string, unknown>) =>
  mutateJson<{ id: number }>("/api/monitors", "POST", payload, object as (v: unknown) => v is { id: number });
/** Read-only rule preview (POST /api/monitors/preview) — "current matches" count. */
export const previewMonitor = (rules: Record<string, unknown>) =>
  mutateJson<{ matched_count: number; trial_ids: string[] }>("/api/monitors/preview", "POST", { rules }, object as (v: unknown) => v is { matched_count: number; trial_ids: string[] });
export const updateMonitor = (id: number, payload: Record<string, unknown>) =>
  mutateJson<{ monitor: MonitorSummary }>(`/api/monitors/${id}`, "PATCH", payload, object as (v: unknown) => v is { monitor: MonitorSummary });
export const deleteMonitor = (id: number) =>
  mutateJson<{ deleted: number }>(`/api/monitors/${id}`, "DELETE", undefined, object as (v: unknown) => v is { deleted: number });
export const runMonitor = (id: number) =>
  mutateJson<{ summary: Record<string, unknown> }>(`/api/monitors/${id}/run`, "POST", undefined, object as (v: unknown) => v is { summary: Record<string, unknown> });
export const getMonitorTrials = (id: number) =>
  fetchJson<{ trials: MonitorTrialRow[] }>(`/api/monitors/${id}/trials`,
    list<MonitorTrialRow>("trials") as unknown as (v: unknown) => v is { trials: MonitorTrialRow[] });
export const getMonitorActivity = (id: number) =>
  fetchJson<{ activity: MonitorActivityRow[] }>(`/api/monitors/${id}/activity`,
    list<MonitorActivityRow>("activity") as unknown as (v: unknown) => v is { activity: MonitorActivityRow[] });
export const getMonitorRuns = (id: number) =>
  fetchJson<{ runs: MonitorRunRow[] }>(`/api/monitors/${id}/runs`,
    list<MonitorRunRow>("runs") as unknown as (v: unknown) => v is { runs: MonitorRunRow[] });

/* ── notifications ────────────────────────────────────────────────────── */
const isNotificationRow = (v: unknown): v is NotificationRow => object(v);
/** Payload guard for GET /api/notifications, shared by the cached read path. */
export const isNotificationList = (v: unknown): v is { notifications: NotificationRow[] } =>
  object(v) && Array.isArray((v as { notifications?: unknown }).notifications);
export const getNotifications = (unread: boolean) =>
  fetchJson<{ notifications: NotificationRow[] }>(`/api/notifications?limit=200${unread ? "&unread=true" : ""}`,
    (v): v is { notifications: NotificationRow[] } => object(v) && Array.isArray((v as { notifications?: unknown }).notifications));
export const getUnreadCount = () =>
  fetchJson<{ count: number }>("/api/notifications/unread-count",
    (v): v is { count: number } => object(v) && typeof (v as { count?: unknown }).count === "number");
export const markNotificationRead = (id: number, read: boolean) =>
  mutateJson<{ id: number }>(`/api/notifications/${id}`, "PATCH", { read }, isNotificationRow as unknown as (v: unknown) => v is { id: number });
export const markAllNotificationsRead = () =>
  mutateJson<{ updated: number }>("/api/notifications/mark-all-read", "POST", undefined, object as (v: unknown) => v is { updated: number });
