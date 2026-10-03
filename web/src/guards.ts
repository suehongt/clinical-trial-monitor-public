/**
 * Hand-written runtime guards over the shapes declared in ./types.
 *
 * Fetched JSON is untyped (`any`); these guards validate it before it reaches
 * the UI so that exporter/viewer drift degrades into an error state instead of
 * a production white screen.
 *
 * Contract:
 *  - every REQUIRED field's primitive type is validated;
 *  - optional fields (`?: ... | null`) accept absent (undefined), null, or the
 *    declared primitive type;
 *  - array element types are validated, including nested
 *    EndpointRow[] / MirrorGroup[] / MirrorTrial[] / ChangeEvent[];
 *  - on any non-conforming input (non-object JSON, null roots, wrong element
 *    types, …) they return false — they never throw.
 */

import type {
  ChangeEvent,
  DailyGroup,
  DayChange,
  DayNewTrial,
  DayTrialChanges,
  DiseaseIndex,
  DiseaseInfo,
  EndpointRow,
  EventsData,
  LiveSearchData,
  MirrorGroup,
  MirrorsData,
  MirrorTrial,
  NctStudyCheck,
  NctStudyDiff,
  ReportData,
  Trial,
  TrialDetailData,
  TrialDetailResponse,
  TrialSearchData,
  WatchInfo,
  WatchListData,
  WatchMatch,
  WatchMutation,
} from "./types";

/* ---------------------------------------------------------------- helpers */

/** Plain JSON object (arrays and null excluded). */
function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isStr(v: unknown): v is string {
  return typeof v === "string";
}

function isNum(v: unknown): v is number {
  return typeof v === "number";
}

/** Every element is a string (empty arrays pass). */
function isStrArray(v: unknown): v is string[] {
  return Array.isArray(v) && v.every(isStr);
}

/** Optional string field per `?: string | null`: absent, null, or string. */
function isOptStr(v: unknown): boolean {
  return v === undefined || v === null || isStr(v);
}

/** Optional number field per `?: number | null`: absent, null, or number. */
function isOptNum(v: unknown): boolean {
  return v === undefined || v === null || isNum(v);
}

/* ------------------------------------------------------------ leaf shapes */

export function isEndpointRow(v: unknown): v is EndpointRow {
  return isObj(v) && isStr(v.no) && isStr(v.indicator) && isStr(v.time) && isStr(v.type);
}

function isEndpointTable(v: unknown): boolean {
  if (v === undefined || v === null) return true; // endpointTable?: ... | null
  return (
    isObj(v) &&
    Array.isArray(v.primary) && v.primary.every(isEndpointRow) &&
    Array.isArray(v.secondary) && v.secondary.every(isEndpointRow)
  );
}

export function isTrial(v: unknown): v is Trial {
  return (
    isObj(v) &&
    // required primitives
    isStr(v.id) &&
    isStr(v.source) &&
    isStr(v.title) &&
    isStr(v.status) &&
    // required string arrays
    isStrArray(v.conditions) &&
    isStrArray(v.sponsors) &&
    isStrArray(v.countries) &&
    isStrArray(v.secondaryEndpoints) &&
    isStrArray(v.interventions) &&
    isStrArray(v.locations) &&
    // optional string fields
    isOptStr(v.scientificTitle) &&
    isOptStr(v.phase) &&
    isOptStr(v.studyType) &&
    isOptStr(v.registrationDate) &&
    isOptStr(v.startDate) &&
    isOptStr(v.completionDate) &&
    isOptStr(v.lastUpdated) &&
    isOptStr(v.url) &&
    isOptStr(v.primaryEndpoint) &&
    // optional number field
    isOptNum(v.enrollment)
  );
}

export function isTrialDetailData(v: unknown): v is TrialDetailData {
  return (
    isObj(v) &&
    // every field is optional (`?: string | null`); endpointTable is nested
    isOptStr(v.summary) &&
    isOptStr(v.investigator) &&
    isOptStr(v.inclusion) &&
    isOptStr(v.exclusion) &&
    isEndpointTable(v.endpointTable)
  );
}

/**
 * GET /api/trials/{source}/{id} — a TrialDetailData carrying the record's
 * per-trial change history.  changeHistory must be absent or an array of
 * ChangeEvent-shaped objects; the envelope fields (generated_at,
 * data_as_of, source, id) are intentionally unvalidated extras.
 */
export function isTrialDetailResponse(v: unknown): v is TrialDetailResponse {
  if (!isObj(v)) return false;
  if (!isTrialDetailData(v)) return false;
  if (v.changeHistory === undefined) return true;
  return Array.isArray(v.changeHistory) && v.changeHistory.every(isChangeEvent);
}

function isDiseaseInfo(v: unknown): v is DiseaseInfo {
  return (
    isObj(v) &&
    isStr(v.key) &&
    isStr(v.label) &&
    isStr(v.label_en) &&
    isNum(v.total) &&
    isStr(v.file)
  );
}

function isMirrorTrial(v: unknown): v is MirrorTrial {
  return (
    isObj(v) &&
    isStr(v.source) &&
    isStr(v.id) &&
    isStr(v.title) &&
    isOptStr(v.status) &&
    isOptNum(v.enrollment) &&
    isOptStr(v.url)
  );
}

function isMirrorGroup(v: unknown): v is MirrorGroup {
  return (
    isObj(v) &&
    isStr(v.masterId) &&
    isStrArray(v.sources) &&
    Array.isArray(v.trials) && v.trials.every(isMirrorTrial)
  );
}

function isChangeEvent(v: unknown): v is ChangeEvent {
  return (
    isObj(v) &&
    // required primitives
    isStr(v.detected_at) &&
    isStr(v.field_name) &&
    isStr(v.id) &&
    isStr(v.source) &&
    isStr(v.title) &&
    // optional string fields
    isOptStr(v.old_value) &&
    isOptStr(v.new_value) &&
    isOptStr(v.change_category) &&
    isOptStr(v.source_url) &&
    // severity fields (schema v10) — optional so static JSON without them
    // (pre-v10 exports) still passes
    isOptStr(v.severity) &&
    isOptStr(v.change_type) &&
    isOptStr(v.importance_score)
  );
}

/* --------------------------------------------------- daily timeline shapes */

function isDayChange(v: unknown): v is DayChange {
  return (
    isObj(v) &&
    isStr(v.field_name) &&
    isStr(v.detected_at) &&
    isOptStr(v.old_value) &&
    isOptStr(v.new_value) &&
    isOptStr(v.change_category)
  );
}

function isDayTrialChanges(v: unknown): v is DayTrialChanges {
  return (
    isObj(v) &&
    isStr(v.id) &&
    isStr(v.source) &&
    isStr(v.title) &&
    isOptStr(v.url) &&
    Array.isArray(v.changes) && v.changes.every(isDayChange)
  );
}

function isDayNewTrial(v: unknown): v is DayNewTrial {
  return (
    isObj(v) &&
    isStr(v.id) &&
    isStr(v.source) &&
    isStr(v.title) &&
    isStr(v.first_crawled_at) &&
    isOptStr(v.url) &&
    isOptStr(v.phase) &&
    isOptNum(v.enrollment)
  );
}

export function isDailyGroup(v: unknown): v is DailyGroup {
  return (
    isObj(v) &&
    isStr(v.date) &&
    isNum(v.change_count) &&
    isNum(v.new_count) &&
    Array.isArray(v.trials) && v.trials.every(isDayTrialChanges) &&
    Array.isArray(v.new_trials) && v.new_trials.every(isDayNewTrial)
  );
}

function isBool(v: unknown): v is boolean {
  return typeof v === "boolean";
}

/** live search — LIVE ClinicalTrials.gov results in Trial shape. */
export function isLiveSearchData(v: unknown): v is LiveSearchData {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    isStr(v.keywords) &&
    isNum(v.total) &&
    Array.isArray(v.trials) && v.trials.every(isTrial)
  );
}

/* --------------------------------------------------------------- watch */

function isWatchMatch(v: unknown): v is WatchMatch {
  return (
    isObj(v) &&
    isStr(v.id) &&
    isStr(v.source) &&
    isStr(v.title) &&
    isOptStr(v.url) &&
    isOptStr(v.first_crawled_at)
  );
}

function isWatchInfo(v: unknown): v is WatchInfo {
  return (
    isObj(v) &&
    isNum(v.watch_id) &&
    isStr(v.keyword) &&
    isStr(v.created_at) &&
    isNum(v.total_matches) &&
    isNum(v.recent_hits) &&
    Array.isArray(v.recent_matches) && v.recent_matches.every(isWatchMatch)
  );
}

export function isWatchListData(v: unknown): v is WatchListData {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    Array.isArray(v.watches) && v.watches.every(isWatchInfo)
  );
}

export function isWatchMutation(v: unknown): v is WatchMutation {
  return isObj(v) && isStr(v.generated_at);
}

/* --------------------------------------------------- live single check */

function isNctStudyDiff(v: unknown): v is NctStudyDiff {
  return (
    isObj(v) &&
    isStr(v.field) &&
    (v.local === undefined || v.local === null || isStr(v.local) || isNum(v.local)) &&
    (v.remote === undefined || v.remote === null || isStr(v.remote) || isNum(v.remote))
  );
}

export function isNctStudyCheck(v: unknown): v is NctStudyCheck {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    isStr(v.nct_id) &&
    isBool(v.in_library) &&
    isTrial(v.live_trial) &&
    Array.isArray(v.diff) && v.diff.every(isNctStudyDiff)
  );
}

/* ------------------------------------------------------------- root guards */

/** index.json — the disease catalogue. */
export function isDiseaseIndex(v: unknown): v is DiseaseIndex {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    Array.isArray(v.diseases) && v.diseases.every(isDiseaseInfo)
  );
}

/** <disease>.json — one disease's full trial report. */
export function isReportData(v: unknown): v is ReportData {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    isStr(v.profile) &&
    isStr(v.label) &&
    isStr(v.label_en) &&
    isNum(v.total) &&
    Array.isArray(v.trials) && v.trials.every(isTrial)
  );
}

/** <disease>_details.json — per-trial detail texts keyed by "<source>:<id>". */
export function isDetailsMap(v: unknown): v is Record<string, TrialDetailData> {
  // An empty map is a valid Record<string, TrialDetailData> (all value fields
  // are optional), so only the object-ness of root and values is required.
  return isObj(v) && Object.values(v).every(isTrialDetailData);
}

/** mirrors.json — cross-source mirror groups. */
export function isMirrorsData(v: unknown): v is MirrorsData {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    isNum(v.total) &&
    Array.isArray(v.groups) && v.groups.every(isMirrorGroup)
  );
}

/** events.json — field-level change events (+ optional day-grouped view). */
export function isEventsData(v: unknown): v is EventsData {
  return (
    isObj(v) &&
    isStr(v.generated_at) &&
    isNum(v.total) &&
    Array.isArray(v.events) && v.events.every(isChangeEvent) &&
    (v.daily === undefined ||
      (Array.isArray(v.daily) && v.daily.every(isDailyGroup)))
  );
}

/** GET /api/trials/search — read-only cross-registry discovery payload. */
export function isTrialSearchData(v: unknown): v is TrialSearchData {
  if (!isObj(v) || !isNum(v.total) || !isNum(v.page) || !isNum(v.page_size)) return false;
  if (!Array.isArray(v.trials) || !v.trials.every(isTrial)) return false;
  const facets = v.facets;
  if (!isObj(facets)) return false;
  const isCounts = (x: unknown): boolean =>
    isObj(x) && Object.values(x).every((n) => typeof n === "number");
  return (
    isCounts(facets.registries) && isCounts(facets.statuses) &&
    isCounts(facets.phases) && isCounts(facets.countries)
  );
}
