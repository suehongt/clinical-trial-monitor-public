import {
  OVERVIEW_SECTION_IDS, clearStructuredFilters, cloneFilterDraft,
  displayMissing, parseDetailTab, parseTrialDensity, toggleTrialSelection,
  trialSelectionKey,
} from "./trialViewState";
import { emptySearchState } from "./searchState";
import type { Trial } from "./types";

const trial = (source: string, id: string): Trial => ({
  source, id, title: `${source} ${id}`, status: "Recruiting", conditions: [],
  sponsors: [], countries: [], secondaryEndpoints: [], interventions: [], locations: [],
});
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };

assert(trialSelectionKey(trial("NCT", "1")) === "NCT:1", "selection key includes source");
assert(trialSelectionKey(trial("ChiCTR", "1")) !== trialSelectionKey(trial("NCT", "1")), "keys cannot collide");
let selected = new Map<string, Trial>();
for (const x of [trial("NCT", "1"), trial("NCT", "2"), trial("NCT", "3")]) selected = toggleTrialSelection(selected, x).selection;
const fourth = toggleTrialSelection(selected, trial("NCT", "4"));
assert(fourth.blocked && fourth.selection.size === 3, "fourth comparison is blocked");
assert(toggleTrialSelection(selected, trial("NCT", "2")).selection.size === 2, "selected item can be removed");
assert(parseTrialDensity("invalid") === "compact", "invalid density falls back");
assert(parseDetailTab("bad") === "overview" && parseDetailTab("changes") === "changes", "tab parsing");
assert(OVERVIEW_SECTION_IDS.length === 7 && new Set(OVERVIEW_SECTION_IDS).size === 7, "section ids are stable and unique");
const state = { ...emptySearchState(), q: "heart", sort: "id" as const, page: 4, registries: ["NCT"], sponsor: ["Acme"] };
const cleared = clearStructuredFilters(state);
assert(cleared.q === "heart" && cleared.sort === "id" && cleared.page === 1, "clear keeps query and sort");
const draft = cloneFilterDraft(state); draft.registries.push("ChiCTR");
assert(state.registries.length === 1, "filter draft does not mutate applied state");
assert(displayMissing(0, "Missing") === "0" && displayMissing(null, "Missing") === "Missing", "zero is not missing");
console.log("trialViewState tests: all passed");
