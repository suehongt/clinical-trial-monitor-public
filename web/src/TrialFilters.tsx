import { useState } from "react";
import type { Translate } from "./i18n";
import { SOURCE_BADGES, phaseLabel } from "./labels";
import {
  PHASE_OPTIONS, REGISTRY_OPTIONS, STATUS_OPTIONS, STUDY_TYPE_OPTIONS,
  type SearchState,
} from "./searchState";
import type { TrialSearchData } from "./types";
import { clearStructuredFilters } from "./trialViewState";

type Facets = TrialSearchData["facets"] | undefined;

export default function TrialFilters({ t, state, facets, onChange, onClear, draft = false }: {
  t: Translate; state: SearchState; facets: Facets;
  onChange: (state: SearchState) => void; onClear?: () => void; draft?: boolean;
}) {
  const active = structuredFilterCount(state);
  const update = (field: ListField, value: string) => {
    const values = state[field];
    onChange({ ...state, [field]: values.includes(value) ? values.filter((x) => x !== value) : [...values, value], page: 1 });
  };
  return <div className={`trial-filters ${draft ? "trial-filters-draft" : ""}`}>
    <div className="trial-filters-head">
      <div><h2>{t("search.filters")}</h2><span>{t("search.activeFilterCount", { n: active })}</span></div>
      <button type="button" className="text-btn" disabled={active === 0}
        onClick={() => onClear ? onClear() : onChange(clearStructuredFilters(state))}>{t("search.clearAllFilters")}</button>
    </div>
    <FacetGroup t={t} label={t("search.filterRegistry")}
      rows={optionRows(REGISTRY_OPTIONS, facets?.registries, state.registries)} active={state.registries}
      onToggle={(value) => update("registries", value)} render={(v) => SOURCE_BADGES[v]?.label ?? v} />
    <FacetGroup t={t} label={t("search.filterStatus")}
      rows={optionRows(STATUS_OPTIONS, facets?.statuses, state.statuses)} active={state.statuses}
      onToggle={(value) => update("statuses", value)} />
    <FacetGroup t={t} label={t("search.filterPhase")}
      rows={optionRows(PHASE_OPTIONS, facets?.phases, state.phase)} active={state.phase}
      onToggle={(value) => update("phase", value)} render={(v) => phaseLabel(v, t)} />
    <FacetGroup t={t} label={t("search.filterStudyType")}
      rows={optionRows(STUDY_TYPE_OPTIONS, undefined, state.studyTypes)} active={state.studyTypes}
      onToggle={(value) => update("studyTypes", value)} />
    <FacetGroup t={t} label={t("search.filterCountry")}
      rows={countryRows(facets?.countries, state.country)} active={state.country}
      onToggle={(value) => update("country", value)} limit={10} />
    {!!facets?.countries_missing && <p className="trial-filter-note">{t("search.countriesMissing", { n: facets.countries_missing })}</p>}
  </div>;
}

type ListField = "registries" | "statuses" | "studyTypes" | "phase" | "country";

export function structuredFilterCount(state: SearchState): number {
  return state.registries.length + state.statuses.length + state.studyTypes.length + state.phase.length +
    state.country.length + state.condition.length + state.intervention.length + state.sponsor.length;
}

function FacetGroup({ t, label, rows, active, onToggle, render = (v) => v, limit = 8 }: {
  t: Translate; label: string; rows: Array<[string, number | null]>; active: string[];
  onToggle: (value: string) => void; render?: (value: string) => string; limit?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? rows : rows.slice(0, limit);
  return <fieldset className="trial-filter-group">
    <legend>{label}</legend>
    <div className="trial-filter-options">
      {shown.map(([value, count]) => <label key={value} className="trial-filter-option">
        <input type="checkbox" checked={active.includes(value)} onChange={() => onToggle(value)} />
        <span>{render(value)}</span>{count !== null && <small>{count.toLocaleString()}</small>}
      </label>)}
    </div>
    {rows.length > limit && <button type="button" className="text-btn trial-filter-more"
      aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
      {expanded ? t("search.showLess") : t("search.showMore", { n: rows.length - limit })}
    </button>}
  </fieldset>;
}

function optionRows(canonical: readonly string[], facets: Record<string, number> | undefined, active: string[]): Array<[string, number | null]> {
  const values = new Set([...canonical, ...Object.keys(facets ?? {}), ...active]);
  return [...values]
    .sort((a, b) => {
      const ai = canonical.indexOf(a), bi = canonical.indexOf(b);
      if (ai >= 0 && bi >= 0) return ai - bi;
      if (ai >= 0) return -1; if (bi >= 0) return 1;
      return a.localeCompare(b);
    })
    .map((value) => [value, facets ? facets[value] ?? 0 : null] as [string, number | null])
    .filter(([value, count]) => count === null || count > 0 || active.includes(value));
}

function countryRows(facets: Record<string, number> | undefined, active: string[]): Array<[string, number | null]> {
  const values = new Set([...Object.keys(facets ?? {}), ...active]);
  return [...values].map((value) => [value, facets?.[value] ?? 0] as [string, number | null])
    .filter(([value, count]) => (count ?? 0) > 0 || active.includes(value))
    .sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0) || a[0].localeCompare(b[0]));
}
