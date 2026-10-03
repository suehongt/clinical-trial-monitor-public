/**
 * Phase 3E.1 search-state conversion tests — the pure-logic half of the
 * search↔monitor contract:
 *
 *   URL params ⇄ SearchState → monitor rule  (no filter may silently vanish)
 *
 * The behavioural half (matcher semantics, round-trips through the real API)
 * lives in tests/test_trial_search.py.
 */
import {
  emptySearchState, isSearchActive, monitorRuleToSearchState,
  searchApiParams, searchStateFromQuery, searchStateToMonitorRule,
  searchStateToQuery, searchStateToSavedState, savedStateToSearchState, suggestedMonitorName,
} from "./searchState";

function assert(cond: boolean, label: string): void {
  if (!cond) throw new Error(`searchState: ${label}`);
}

function deepEqual(actual: unknown, expected: unknown, label: string): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${label}: expected ${e}, got ${a}`);
}

// ── URL → state → URL round-trip (#14/#46) ────────────────────────────────

const full = new URLSearchParams(
  "q=myocarditis&registries=NCT,ChiCTR&statuses=Recruiting&study_types=Observational" +
  "&phase=Phase%202&country=United%20States&condition=Heart%20Failure" +
  "&intervention=Device%3A%20LVAD&sponsor=Acme&sort=start_date&page=3");
const state = searchStateFromQuery(full);
deepEqual(state, {
  q: "myocarditis", registries: ["NCT", "ChiCTR"], statuses: ["Recruiting"],
  studyTypes: ["Observational"], phase: ["Phase 2"], country: ["United States"],
  condition: ["Heart Failure"], intervention: ["Device: LVAD"], sponsor: ["Acme"],
  sort: "start_date", page: 3,
}, "full state parses");

const roundTrip = searchStateFromQuery(new URLSearchParams(searchStateToQuery(state)));
deepEqual(roundTrip, state, "state survives URL round-trip");

// defaults omit cleanly — empty state serializes to an empty query string
deepEqual(searchStateToQuery(emptySearchState()), "", "empty state is a clean URL");
const savedState = searchStateToSavedState(state);
deepEqual(savedStateToSearchState(savedState), { ...state, page: 1 },
  "saved state restores all executable filters and sort but resets pagination");
deepEqual(searchStateToMonitorRule(savedStateToSearchState(savedState)), ruleFromState(state),
  "saved state to monitor retains every executable field");
function ruleFromState(s: typeof state) { return searchStateToMonitorRule(s); }
const commaStatus = { ...emptySearchState(), q: "heart failure", statuses: ["Active, not recruiting"] };
deepEqual(searchStateFromQuery(new URLSearchParams(searchStateToQuery(commaStatus))), commaStatus,
  "comma-bearing canonical status survives URL round-trip");

// ── state → monitor rule (#20/#21) ────────────────────────────────────────

const rule = searchStateToMonitorRule(state);
deepEqual(rule, {
  query: ["myocarditis"], registries: ["NCT", "ChiCTR"], statuses: ["Recruiting"],
  study_types: ["Observational"], phase: ["Phase 2"], country: ["United States"],
  condition: ["Heart Failure"], intervention: ["Device: LVAD"], sponsor: ["Acme"],
}, "rule carries every dimension with search-state semantics");

// a minimal search produces a compact rule with no silent defaults
deepEqual(
  searchStateToMonitorRule({ ...emptySearchState(), q: "myocarditis car-t" }),
  { query: ["myocarditis car-t"] },
  "minimal state → compact rule",
);

// ── monitor rule → search state (#28 Open as Search) ──────────────────────

const reopened = monitorRuleToSearchState(rule);
deepEqual(
  { ...reopened, page: state.page, sort: state.sort },
  state,
  "rule → state restores the same search (sort/page reset to defaults)",
);
deepEqual(
  searchStateToMonitorRule(reopened),
  rule,
  "rule → state → rule is lossless",
);
// string-valued rules (free-form editor) parse like array-valued ones
deepEqual(
  monitorRuleToSearchState({ query: "heart failure", statuses: "Recruiting" }),
  { ...emptySearchState(), q: "heart failure", statuses: ["Recruiting"] },
  "string rule values are accepted",
);
// a condition-only rule (free-form monitor) opens as a condition search
deepEqual(
  monitorRuleToSearchState({ condition: "heart failure" }),
  { ...emptySearchState(), condition: ["heart failure"] },
  "condition rule maps to the lossless condition dimension",
);
// empty/null rules give an empty state
deepEqual(monitorRuleToSearchState(null), emptySearchState(), "null rules → empty state");

// ── API params (#5) ────────────────────────────────────────────────────────

const params = new URLSearchParams(searchApiParams(state));
assert(params.get("registries") === "NCT,ChiCTR", "api params join registries");
assert(params.get("statuses") === "Recruiting", "api params join statuses");
assert(params.get("sort") === "start_date" && params.get("page") === "3", "api params carry sort/page");
assert(params.get("page_size") === "25", "api params default page size");
const emptyParams = new URLSearchParams(searchApiParams(emptySearchState()));
assert(emptyParams.get("q") === null && emptyParams.get("registries") === null,
  "empty state sends no filter params");

// ── activity + naming ──────────────────────────────────────────────────────

assert(isSearchActive(state), "full state counts as active");
assert(!isSearchActive(emptySearchState()), "empty state is not active");
assert(isSearchActive({ ...emptySearchState(), registries: ["NCT"] }),
  "filter-only state is active");
assert(suggestedMonitorName(state) === "myocarditis", "name defaults to the query");
assert(suggestedMonitorName(emptySearchState()).length > 0, "name has a fallback");

console.log("searchState tests: all passed");
