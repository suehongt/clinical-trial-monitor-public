export interface EndpointRow {
  no: string;
  indicator: string;
  time: string;
  type: string;
}

export interface Trial {
  id: string;
  source: string;
  title: string;
  scientificTitle?: string | null;
  status: string;
  phase?: string | null;
  studyType?: string | null;
  enrollment?: number | null;
  registrationDate?: string | null;
  startDate?: string | null;
  completionDate?: string | null;
  lastUpdated?: string | null;
  conditions: string[];
  sponsors: string[];
  countries: string[];
  url?: string | null;
  primaryEndpoint?: string | null;
  secondaryEndpoints: string[];
  interventions: string[];
  locations: string[];
}

/** Heavy per-trial detail text — lazy-loaded from <key>_details.json */
export interface TrialDetailData {
  summary?: string | null;
  investigator?: string | null;
  inclusion?: string | null;
  exclusion?: string | null;
  endpointTable?: { primary: EndpointRow[]; secondary: EndpointRow[] } | null;
}

/**
 * API per-trial detail response (GET /api/trials/{source}/{id}):
 * a TrialDetailData plus the record's full change timeline and the date it
 * entered the monitor library (origin node of the timeline).
 */
export interface TrialDetailResponse extends TrialDetailData {
  changeHistory?: ChangeEvent[];
  first_crawled_at?: string | null;
}

/**
 * LIVE search (GET /api/nct/live, Phase 7 P2): results straight from the
 * ClinicalTrials.gov registry — fresher than the local library snapshot.
 * Trials carry the standard Trial shape plus a registry url.
 */
export interface LiveSearchData {
  generated_at: string;
  keywords: string;
  live: boolean;
  cached?: boolean;
  total: number;
  trials: Trial[];
}

/** One registered keyword subscription with live library match counts */
export interface WatchMatch {
  id: string;
  source: string;
  title: string;
  url?: string | null;
  first_crawled_at?: string | null;
}

export interface WatchInfo {
  watch_id: number;
  keyword: string;
  created_at: string;
  total_matches: number;
  recent_hits: number;
  recent_matches: WatchMatch[];
}

export interface WatchListData {
  generated_at: string;
  watches: WatchInfo[];
}

export interface WatchMutation {
  generated_at: string;
}

/** One field differing between the live registry and the local record */
export interface NctStudyDiff {
  field: string;
  local?: string | number | null;
  remote?: string | number | null;
}

/** LIVE single-trial check (GET /api/nct/study/{id}) */
export interface NctStudyCheck {
  generated_at: string;
  nct_id: string;
  in_library: boolean;
  live_trial: Trial;
  diff: NctStudyDiff[];
  cached?: boolean;
}

export interface ReportData {
  generated_at: string;
  /** API payloads only: per-source last successful sync (static JSON omits it) */
  data_as_of?: Record<string, string | null>;
  profile: string;
  label: string;
  label_en: string;
  total: number;
  trials: Trial[];
}

export interface DiseaseInfo {
  key: string;
  label: string;
  label_en: string;
  total: number;
  file: string;
}

export interface DiseaseIndex {
  generated_at: string;
  data_as_of?: Record<string, string | null>;
  diseases: DiseaseInfo[];
}

/** Light per-source record inside a cross-source mirror group */
export interface MirrorTrial {
  source: string;
  id: string;
  title: string;
  status?: string | null;
  enrollment?: number | null;
  url?: string | null;
}

export interface MirrorGroup {
  masterId: string;
  sources: string[];
  trials: MirrorTrial[];
}

export interface MirrorsData {
  generated_at: string;
  data_as_of?: Record<string, string | null>;
  total: number;
  groups: MirrorGroup[];
}

export interface ChangeEvent {
  detected_at: string;
  field_name: string;
  old_value?: string | null;
  new_value?: string | null;
  change_category?: string | null;
  severity?: string | null;
  change_type?: string | null;
  importance_score?: string | null;
  id: string;
  source: string;
  title: string;
  source_url?: string | null;
  /** API payloads only: server-side human-readable field labels (Phase 3E) */
  field_label?: string | null;
  field_label_zh?: string | null;
}

/** One field-level diff inside a day/trial group */
export interface DayChange {
  field_name: string;
  old_value?: string | null;
  new_value?: string | null;
  change_category?: string | null;
  detected_at: string;
  field_label?: string | null;
  field_label_zh?: string | null;
}

/** All changes of one trial within one day */
export interface DayTrialChanges {
  id: string;
  source: string;
  title: string;
  url?: string | null;
  changes: DayChange[];
}

/** A trial first registered on that day (digest "new trial" semantics) */
export interface DayNewTrial {
  id: string;
  source: string;
  title: string;
  url?: string | null;
  enrollment?: number | null;
  phase?: string | null;
  first_crawled_at: string;
}

/** One day of activity: field diffs grouped per trial + new registrations */
export interface DailyGroup {
  date: string;
  change_count: number;
  new_count: number;
  trials: DayTrialChanges[];
  new_trials: DayNewTrial[];
}

export interface EventsData {
  generated_at: string;
  data_as_of?: Record<string, string | null>;
  total: number;
  events: ChangeEvent[];
  /** API payloads only: day-grouped render model (static mode groups client-side) */
  daily?: DailyGroup[];
}

export type Severity = "critical" | "important" | "normal" | "minor";
export interface WatchPreference { field_group: string; enabled: boolean; minimum_severity: Severity; }
export interface TrialWatch { id: number; trial_id: string; enabled: boolean; preferences: WatchPreference[]; last_viewed_at?: string | null; last_change_seen_at?: string | null; }
export interface WatchedTrial extends TrialWatch { title: string; current_status?: string | null; registries: string[]; last_changed_at?: string | null; unseen_change_count: number; highest_unseen_severity?: Severity | null; }
export interface TrialEvent extends ChangeEvent { event_id: number; field_group: string; source_updated_at?: string | null; from_version: number; to_version: number; field_label?: string | null; field_label_zh?: string | null; }
export interface TimelineGroup { record_id: number; to_version: number; detected_at: string; source: string; severity: Severity; events: TrialEvent[]; }
export interface TrialVersion { id: number; version_number: number; registry: string; retrieved_at?: string | null; source_updated_at?: string | null; hash?: string | null; change_count: number; highest_change_severity?: Severity | null; }
export interface VersionDifference { field_name: string; field_group: string; old_value?: unknown; new_value?: unknown; change_type: string; absolute_change?: number; percent_change?: number; date_shift_days?: number; field_label?: string | null; field_label_zh?: string | null; }

/* ── Phase 3E: dashboard / trial-detail page / monitor product surfaces ── */

/** GET /api/dashboard?window=24h|7d|30d — the whole landing payload. */
export interface DashboardData {
  generated_at: string;
  data_as_of?: Record<string, string | null>;
  window: "24h" | "7d" | "30d";
  summary: { new_trials: number; updated_trials: number; important_changes: number; watched_updates: number };
  updates: Array<TrialEvent & { watched?: boolean; title: string }>;
  monitors: Array<{
    id: number; name: string; enabled: boolean; schedule_enabled: boolean;
    schedule_frequency?: string | null; next_run_at?: string | null; last_checked_at?: string | null;
    current_trials: number; new_count: number; changed_count: number; left_count: number;
    last_run_at?: string | null;
  }>;
  watched_activity: Array<{
    trial_id: string; title?: string | null; current_status?: string | null;
    registries: string[]; last_changed_at?: string | null;
    change_count: number; highest_severity?: Severity | null;
  }>;
}

/** GET /api/trials/{source}/{id} — full trial-detail page payload. */
export interface TrialDetailFull extends TrialDetailData {
  trial?: Trial;
  monitors?: Array<{ id: number; name: string }>;
  changeHistory?: ChangeEvent[];
  first_crawled_at?: string | null;
  /** GET /api/trials/{source}/{id}/mirrors — cross-source sibling records */
  siblings?: Array<{
    short_name?: string; source_trial_id?: string; title?: string | null;
    status?: string | null; enrollment?: number | null;
    last_updated_at_source?: string | null; source_url?: string | null;
  }>;
}

/** Monitor as listed by GET /api/monitors (+ runs/enrichment from the API). */
export interface MonitorSummary {
  id: number; name: string; enabled: boolean | number;
  monitor_type?: string; description?: string | null;
  schedule_enabled?: boolean | number; schedule_frequency?: string | null;
  next_run_at?: string | null; last_checked_at?: string | null;
  current_trials: number; new_count: number; changed_count: number; left_count: number;
  email_notifications_enabled?: boolean | number; email_recipient?: string | null;
  created_at?: string; updated_at?: string;
  last_run_at?: string | null; rules?: Record<string, string | string[]>;
}

/** One row of GET /api/monitors/{id}/trials */
export interface MonitorTrialRow {
  id: number; monitor_id: number; trial_id: string;
  currently_matches: boolean | number;
  first_matched_at?: string; last_matched_at?: string; left_at?: string | null;
  title?: string | null; source?: string; status?: string | null;
  last_updated_at_source?: string | null;
}

/** One row of GET /api/monitors/{id}/activity (joined with change fields) */
export interface MonitorActivityRow {
  id: number; monitor_id: number; trial_id: string;
  event_type: string; detected_at: string;
  metadata?: string | null;
  /** registry short name of the trial (subquery) — powers correct detail links */
  source?: string | null;
  field_name?: string | null; old_value?: string | null; new_value?: string | null;
  severity?: Severity | null; change_type?: string | null;
  trial_event_id?: number | null;
  field_label?: string | null; field_label_zh?: string | null;
}

/**
 * GET /api/trials/search (Phase 3E.1) — read-only cross-registry discovery
 * over the local corpus using monitor-rule semantics.  `total` and the
 * facets describe the FULL matching set (never just the returned page);
 * `unfiltered_total` is set only when structured filters emptied out a
 * query that matches globally.
 */
export interface TrialSearchData {
  generated_at: string;
  data_as_of?: Record<string, string | null>;
  rules: Record<string, string[]>;
  expanded_query?: string[] | null;
  /** 中文复合词 0 命中时自动拆词扩展的段（如 结直肠癌甲基化 → [结直肠癌, 甲基化]） */
  segmented_query?: string[] | null;
  total: number;
  unfiltered_total?: number | null;
  page: number;
  page_size: number;
  sort: string;
  trials: Trial[];
  facets: {
    registries: Record<string, number>;
    statuses: Record<string, number>;
    phases: Record<string, number>;
    countries: Record<string, number>;
    /** matches with no recruitment-country data — they appear in no country bucket */
    countries_missing?: number;
  };
}

/** One row of GET /api/monitors/{id}/runs */
export interface MonitorRunRow {
  id: number; monitor_id: number; started_at: string; completed_at?: string | null;
  status: string; matched_count: number; new_count: number; changed_count: number;
  left_count: number; error_message?: string | null; trigger?: string; scheduled_for?: string | null;
}

/** In-app notification row (GET /api/notifications) */
export interface NotificationRow {
  id: number; summary?: string | null; event_type: string; created_at: string;
  read_at?: string | null; monitor_name?: string | null; trial_id?: string | null;
  trial_title?: string | null;
  source?: string | null;
  email_status?: string | null; email_notification_id?: number | null;
  trial_event_id?: number | null;
}

/** One deduplicated persisted trial change in the scoped Updates feed. */
export interface ScopedUpdateItem {
  event_id: number; trial_id: string; title: string; source: string;
  field_name: string; field_label?: string | null; field_label_zh?: string | null;
  old_value?: string | null; new_value?: string | null;
  change_category?: string | null; change_type?: string | null;
  severity: Severity; detected_at: string; watched: boolean; monitors: string[];
  project_trial?: boolean; project_monitors?: string[];
}

export interface ScopedUpdatesData {
  scope: "monitored" | "watched" | "monitor" | "all" | "project";
  monitor_id?: number | null; monitor_name?: string | null;
  project_id?: number | null;
  window: "24h" | "7d" | "30d" | "all";
  registry?: string | null; severity?: Severity | "priority" | null; category?: string | null;
  total: number; trial_count: number; page: number; page_size: number;
  items: ScopedUpdateItem[];
  facets: { registries: Record<string, number>; severities: Record<string, number> };
  analytics?: { trends: Array<{ date: string; critical: number; important: number; normal: number; minor: number }>; categories: Record<string, number> };
}

/* ── Live source check (POST /api/trials/live-check, ChiCTR/CTR/NCT) ──── */

export interface LiveCheckEntry {
  source_trial_id: string;
  title: string;
  url: string | null;
  in_library?: boolean;
  /** WHO primary-registry provenance; not the trial's recruitment country. */
  source_registry?: string;
  source_jurisdiction?: string;
}

export interface LiveCheckResult {
  source: string;
  status: "ok" | "cached" | "cooldown" | "circuit_open" | "error" | "unsupported";
  found: number;
  shown?: number;
  queued: number;
  enriched: number;
  pages_walked?: number;
  stopped_reason?: string;
  site_total?: number | null;
  has_more?: boolean;
  retry_after?: number;
  error?: string;
  query_used?: string;
  /** 本源本次使用的原站检索字段（title/secsponsor/keywords/indication；拆词回退时为 null） */
  field_used?: string | null;
  entries: LiveCheckEntry[];
}

export interface LiveCheckResponse {
  generated_at: string;
  q: string;
  results: LiveCheckResult[];
}
