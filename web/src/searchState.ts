/**
 * Search state — the single conversion path between three representations
 * of one trial query (Phase 3E.1):
 *
 *     URL query string  ⇄  SearchState  →  monitor rule (JSON)
 *                                        →  /api/trials/search params
 *
 * Search must behave like an unsaved monitor preview, so the rule produced
 * by `searchStateToMonitorRule` uses the exact backend monitor rule schema
 * (core/monitors.py SUPPORTED_RULES) — saving a search as a monitor loses
 * no filter, and reopening a monitor as a search reproduces the same state.
 */

/** Canonical registry short names (db registry_sources / SOURCE_BADGES). */
export const REGISTRY_OPTIONS = ["NCT", "ChiCTR", "CTR", "CTIS", "ISRCTN", "EUCTR", "ICTRP"] as const;

/**
 * Normalized recruitment-status labels (db status_types seeds).  Values are
 * the backend's own — the matcher compares them case-insensitively against
 * status_types.label, so the UI must offer exactly these, never a private
 * vocabulary.
 */
export const STATUS_OPTIONS = [
  "Recruiting", "Not yet recruiting", "Active, not recruiting",
  "Enrolling by invitation", "Completed", "Terminated", "Suspended",
  "Withdrawn",
] as const;

/**
 * Canonical phase buckets as emitted by the server's phase facets
 * (core.monitors.normalize_phase).  Filter terms use substring semantics, so
 * the old spellings ("Phase 1/Phase 2") keep matching; these are the facet
 * values the UI offers.
 */
export const PHASE_OPTIONS = [
  "Phase 1", "Phase 1/2", "Phase 1/2/3/4", "Phase 2", "Phase 2/3",
  "Phase 3", "Phase 3/4", "Phase 4", "N/A", "Other",
] as const;

/** Normalized study types (db study_types seeds, curated for the UI). */
export const STUDY_TYPE_OPTIONS = ["Interventional", "Observational"] as const;

export const SEARCH_SORTS = ["last_updated", "start_date", "id"] as const;
export type SearchSort = (typeof SEARCH_SORTS)[number];

export interface SearchState {
  /** Free-text query — whitespace-split AND terms in the shared matcher. */
  q: string;
  /** Canonical registry short names; empty = all registries. */
  registries: string[];
  /** Normalized status labels; empty = any status. */
  statuses: string[];
  /** Normalized study type labels; empty = any type. */
  studyTypes: string[];
  /** Phase terms (substring semantics — "Phase 2" matches "Phase 1/Phase 2"). */
  phase: string[];
  /** Country terms (substring semantics). */
  country: string[];
  /** Rule dimensions without dedicated filter UI, carried losslessly. */
  condition: string[];
  intervention: string[];
  sponsor: string[];
  sort: SearchSort;
  page: number;
}

export const DEFAULT_PAGE_SIZE = 25;

export function emptySearchState(): SearchState {
  return {
    q: "", registries: [], statuses: [], studyTypes: [], phase: [],
    country: [], condition: [], intervention: [], sponsor: [],
    sort: "last_updated", page: 1,
  };
}

const listParam = (raw: string | null): string[] =>
  (raw ?? "").split(",").map((s) => s.replace(/%2C/gi, ",").trim()).filter(Boolean);

const toSort = (raw: string | null): SearchSort =>
  (SEARCH_SORTS as readonly string[]).includes(raw ?? "") ? (raw as SearchSort) : "last_updated";

/** Parse `/trials?...` query params into SearchState (URL is the truth). */
export function searchStateFromQuery(query: URLSearchParams): SearchState {
  return {
    q: (query.get("q") ?? "").trim(),
    registries: listParam(query.get("registries")),
    statuses: listParam(query.get("statuses")),
    studyTypes: listParam(query.get("study_types")),
    phase: listParam(query.get("phase")),
    country: listParam(query.get("country")),
    condition: listParam(query.get("condition")),
    intervention: listParam(query.get("intervention")),
    sponsor: listParam(query.get("sponsor")),
    sort: toSort(query.get("sort")),
    page: Math.max(1, Number(query.get("page") || "1") || 1),
  };
}

function appendList(params: URLSearchParams, key: string, values: string[]): void {
  if (values.length > 0) params.set(key, values.map((v) => v.replace(/,/g, "%2C")).join(","));
}

/** Persist only executable filters plus sort; page is a transient view state. */
export function searchStateToSavedState(state: SearchState): Record<string, string | string[]> {
  const saved: Record<string, string | string[]> = { sort: state.sort };
  if (state.q) saved.q = state.q;
  for (const [key, values] of [
    ["registries", state.registries], ["statuses", state.statuses],
    ["study_types", state.studyTypes], ["phase", state.phase], ["country", state.country],
    ["condition", state.condition], ["intervention", state.intervention], ["sponsor", state.sponsor],
  ] as Array<[string, string[]]>) if (values.length) saved[key] = [...values];
  return saved;
}

export function savedStateToSearchState(saved: Record<string, unknown>): SearchState {
  return { ...monitorRuleToSearchState({ ...saved, query: saved.q }),
    sort: toSort(typeof saved.sort === "string" ? saved.sort : null), page: 1 };
}

/** Serialize SearchState back to a `/trials?...` query string (no path). */
export function searchStateToQuery(state: SearchState): string {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  appendList(p, "registries", state.registries);
  appendList(p, "statuses", state.statuses);
  appendList(p, "study_types", state.studyTypes);
  appendList(p, "phase", state.phase);
  appendList(p, "country", state.country);
  appendList(p, "condition", state.condition);
  appendList(p, "intervention", state.intervention);
  appendList(p, "sponsor", state.sponsor);
  if (state.sort !== "last_updated") p.set("sort", state.sort);
  if (state.page > 1) p.set("page", String(state.page));
  return p.toString();
}

/** `/api/trials/search` request params for this state. */
export function searchApiParams(state: SearchState, pageSize = DEFAULT_PAGE_SIZE): string {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  appendList(p, "registries", state.registries);
  appendList(p, "statuses", state.statuses);
  appendList(p, "study_types", state.studyTypes);
  appendList(p, "phase", state.phase);
  appendList(p, "country", state.country);
  appendList(p, "condition", state.condition);
  appendList(p, "intervention", state.intervention);
  appendList(p, "sponsor", state.sponsor);
  p.set("sort", state.sort);
  p.set("page", String(state.page));
  p.set("page_size", String(pageSize));
  return p.toString();
}

/** A search is active when any query text or filter constrains the corpus. */
export function isSearchActive(state: SearchState): boolean {
  return Boolean(
    state.q || state.registries.length || state.statuses.length ||
    state.studyTypes.length || state.phase.length || state.country.length ||
    state.condition.length || state.intervention.length || state.sponsor.length,
  );
}

/**
 * The compact monitor rule for this search state — the exact semantics the
 * user saw.  Empty dimensions are omitted so the persisted rule carries no
 * silent defaults, and round-tripping through GET /api/monitors/{id}
 * reproduces it byte-for-byte.
 */
export function searchStateToMonitorRule(state: SearchState): Record<string, string[]> {
  const rule: Record<string, string[]> = {};
  if (state.q) rule.query = [state.q];
  if (state.registries.length) rule.registries = [...state.registries];
  if (state.statuses.length) rule.statuses = [...state.statuses];
  if (state.studyTypes.length) rule.study_types = [...state.studyTypes];
  if (state.phase.length) rule.phase = [...state.phase];
  if (state.country.length) rule.country = [...state.country];
  if (state.condition.length) rule.condition = [...state.condition];
  if (state.intervention.length) rule.intervention = [...state.intervention];
  if (state.sponsor.length) rule.sponsor = [...state.sponsor];
  return rule;
}

const asList = (v: unknown): string[] => {
  if (Array.isArray(v)) return v.map((x) => String(x).trim()).filter(Boolean);
  const s = typeof v === "string" ? v.trim() : "";
  return s ? [s] : [];
};

/** Inverse path: a persisted monitor rule becomes an editable search (#28). */
export function monitorRuleToSearchState(rules: Record<string, unknown> | null | undefined): SearchState {
  const r = rules ?? {};
  const state: SearchState = emptySearchState();
  state.q = asList(r.query).join(" ");
  state.registries = asList(r.registries);
  state.statuses = asList(r.statuses);
  state.studyTypes = asList(r.study_types);
  state.phase = asList(r.phase);
  state.country = asList(r.country);
  state.condition = asList(r.condition);
  state.intervention = asList(r.intervention);
  state.sponsor = asList(r.sponsor);
  return state;
}

/** Suggested monitor name for a tracked search ("Myocarditis", …). */
export function suggestedMonitorName(state: SearchState): string {
  return state.q || [...state.statuses, ...state.registries].join(" ") || "Trial search";
}
