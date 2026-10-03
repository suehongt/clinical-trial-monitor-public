import type { Trial } from "./types";
import type { SearchState } from "./searchState";

export type TrialDensity = "comfortable" | "compact";
export type DetailTab = "overview" | "changes" | "history" | "sources";

export const DETAIL_TABS: DetailTab[] = ["overview", "changes", "history", "sources"];
export const OVERVIEW_SECTION_IDS = [
  "study", "conditions-interventions", "outcomes", "eligibility",
  "locations", "sponsor-collaborators", "provenance",
] as const;

export function trialSelectionKey(trial: Pick<Trial, "source" | "id">): string {
  return `${trial.source}:${trial.id}`;
}

export function parseTrialDensity(value: unknown): TrialDensity {
  return value === "compact" || value === "comfortable" ? value : "compact";
}

export function parseDetailTab(value: string | null | undefined): DetailTab {
  return DETAIL_TABS.includes(value as DetailTab) ? value as DetailTab : "overview";
}

export function toggleTrialSelection(
  current: ReadonlyMap<string, Trial>,
  trial: Trial,
  max = 3,
): { selection: Map<string, Trial>; blocked: boolean } {
  const selection = new Map(current);
  const key = trialSelectionKey(trial);
  if (selection.has(key)) {
    selection.delete(key);
    return { selection, blocked: false };
  }
  if (selection.size >= max) return { selection, blocked: true };
  selection.set(key, trial);
  return { selection, blocked: false };
}

export function clearStructuredFilters(state: SearchState): SearchState {
  return {
    ...state,
    registries: [], statuses: [], studyTypes: [], phase: [], country: [],
    condition: [], intervention: [], sponsor: [], page: 1,
  };
}

export function cloneFilterDraft(state: SearchState): SearchState {
  return {
    ...state,
    registries: [...state.registries], statuses: [...state.statuses],
    studyTypes: [...state.studyTypes], phase: [...state.phase],
    country: [...state.country], condition: [...state.condition],
    intervention: [...state.intervention], sponsor: [...state.sponsor],
  };
}

export function displayMissing(value: unknown, missing: string): string {
  if (value === null || value === undefined || value === "") return missing;
  return String(value);
}
