/**
 * Trials page (Phase 3E / 3E.1): the trial library with three surfaces.
 *
 * - All (browse, no active search): profile-scoped list with registry/status
 *   chips and a local text filter; in static mode this also degrades gently
 *   when a query is typed with the backend down.
 * - All (active search): mounted in SearchResults.tsx — URL-driven,
 *   backend-side discovery via /api/trials/search (read-only), structured
 *   refinements, watch actions and Track-this-search.
 * - Watched (API mode): persisted watches with unseen-change counts.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { refreshableUrl, useJson, useStickyJson } from "./api";
import type {
  DiseaseIndex, EventsData, ReportData, Trial, TrialDetailData, TrialDetailResponse,
  WatchedTrial,
} from "./types";
import { isDetailsMap, isDiseaseIndex, isEventsData, isReportData, isTrialDetailResponse } from "./guards";
import { isWatchList, unwatchTrial, watchTrial } from "./monitoringApi";
import { SOURCE_BADGES, phaseLabel, statusKey, statusTone } from "./labels";
import { EmptyState, Loading } from "./ui";
import TrialDetailModal from "./TrialDetail";
import type { Lang, Translate } from "./i18n";
import type { DataSource } from "./datasource";
import { asOfText, detailsUrl, indexUrl, trialsUrl } from "./datasource";
import { navigate, useRoute } from "./router";
import SearchResults from "./SearchResults";
import { TrialComparison } from "./SearchResults";
import TrialRow from "./TrialRow";
import TrialPreviewDrawer from "./TrialPreviewDrawer";
import TrialCompareBar from "./TrialCompareBar";
import { toggleTrialSelection, trialSelectionKey } from "./trialViewState";
import NaturalSearch from "./NaturalSearch";
import SavedSearchesPage from "./SavedSearchesPage";
import { emptySearchState, searchStateFromQuery, searchStateToQuery } from "./searchState";

const SORT_KEYS = ["lastUpdated", "registration", "enrollment", "id"] as const;
type SortKey = (typeof SORT_KEYS)[number];

function toSortKey(v: string): SortKey {
  return SORT_KEYS.find((k) => k === v) ?? "lastUpdated";
}

function fmtEnrollment(n?: number | null): string {
  return typeof n === "number" ? n.toLocaleString() : "—";
}

/** UTC ms of a "YYYY-MM-DD HH:MM:SS" registry stamp (NaN when absent). */
function stampMs(s: string | null | undefined): number {
  if (!s) return Number.NaN;
  return Date.parse(s.slice(0, 10) + "T" + (s.slice(11, 19) || "00:00:00") + "Z");
}

export default function TrialsPage({ t, lang, dataSource }: { t: Translate; lang: Lang; dataSource: DataSource }) {
  const route = useRoute();
  const isWatchedView = route.query.get("view") === "watched";
  const isSavedView = route.query.get("view") === "saved";
  const api = dataSource === "api";

  // URL is the single source of truth for search state (reload/share safe)
  const search = useMemo(() => searchStateFromQuery(route.query), [route.url]);

  const [indexChoice, setIndexChoice] = useState<string | null>(null);
  const indexState = useJson<DiseaseIndex>(indexUrl(dataSource), isDiseaseIndex);
  const index = indexState.status === "ready" ? indexState.data : null;
  const profile = indexChoice ?? (index ? index.diseases[0]?.key ?? "" : "");

  const reportState = useJson<ReportData>(
    dataSource && profile ? trialsUrl(dataSource, profile) : null, isReportData);
  const data = reportState.status === "ready" ? reportState.data : null;

  const eventsState = useJson<EventsData>(dataSource ? "/api/events?days=7&limit=0" : null, isEventsData);
  const events = eventsState.status === "ready" ? eventsState.data : null;
  const sourceHealth = useJson<{ registries: Array<{ freshness: string; full_name: string }> }>(
    api ? `/api/registries/status?context=${encodeURIComponent(route.url)}` : null,
    (v): v is { registries: Array<{ freshness: string; full_name: string }> } => typeof v === "object" && v !== null && Array.isArray((v as { registries?: unknown }).registries),
  );
  const staleSources = sourceHealth.status === "ready" ? sourceHealth.data.registries.filter((s) => s.freshness === "stale" || s.freshness === "delayed") : [];

  // browse-view local text filter (also the static-mode fallback)
  const [query, setQuery] = useState(() => route.query.get("q") ?? "");
  useEffect(() => { setQuery(route.query.get("q") ?? ""); }, [route.url]);

  // watched ids for inline watch toggles and the watched feed share one
  // cached GET: revisits render instantly from the cache and revalidate
  // silently; watch/unwatch bumps the tick to force a fresh fetch, and the
  // sticky hook keeps the current rows/ids visible while it runs
  const [watchTick, setWatchTick] = useState(0);
  const watches = useStickyJson<{ generated_at: string; watches: WatchedTrial[] }>(
    api ? refreshableUrl("/api/watches", watchTick) : null, isWatchList);
  const watchedIds = useMemo(
    () => new Set((watches.data?.watches ?? []).map((w) => w.trial_id)),
    [watches.data],
  );

  const toggleWatch = (trial: Trial) => {
    if (!api) return;
    const watching = watchedIds.has(trial.id);
    const call = watching ? unwatchTrial(trial.source, trial.id) : watchTrial(trial.source, trial.id);
    call.then(() => setWatchTick((n) => n + 1)).catch(() => undefined);
  };

  // watched feed (watched view only) — same cached response as the ids above
  const watchedRows = api && isWatchedView ? watches.data?.watches ?? null : null;
  const watchedErr = api && isWatchedView ? watches.error : null;

  // NEW/CHANGED flags (7d) keyed by trial id
  const recentFlags = useMemo(() => {
    const cutoff = Date.now() - 7 * 86400_000;
    const changed = new Set<string>();
    for (const e of events?.events ?? []) {
      const ts = stampMs(e.detected_at);
      if (!Number.isNaN(ts) && ts >= cutoff) changed.add(e.id);
    }
    const added = new Set<string>();
    for (const d of events?.daily ?? []) {
      for (const n of d.new_trials) {
        const ts = stampMs(n.first_crawled_at);
        if (!Number.isNaN(ts) && ts >= cutoff) added.add(n.id);
      }
    }
    return { changed, added };
  }, [events]);

  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const x of data?.trials ?? []) counts.set(x.source, (counts.get(x.source) ?? 0) + 1);
    return counts;
  }, [data]);

  const [source, setSource] = useState("All");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState<SortKey>("lastUpdated");
  const [visible, setVisible] = useState(60);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    let rows = data.trials.filter((x: Trial) => {
      if (source !== "All" && x.source !== source) return false;
      if (status !== "all" && statusKey(x.status) !== status) return false;
      if (!q) return true;
      const hay = `${x.id} ${x.title} ${x.scientificTitle ?? ""} ${x.conditions.join(" ")} ${x.sponsors.join(" ")}`;
      return hay.toLowerCase().includes(q);
    });
    rows = [...rows].sort((a, b) => {
      if (sort === "enrollment") return (b.enrollment ?? 0) - (a.enrollment ?? 0);
      if (sort === "lastUpdated") return (b.lastUpdated ?? "").localeCompare(a.lastUpdated ?? "");
      if (sort === "registration") return (b.registrationDate ?? "").localeCompare(a.registrationDate ?? "");
      return a.id.localeCompare(b.id);
    });
    return rows;
  }, [data, query, source, status, sort]);

  const shown = filtered.slice(0, visible);

  // static-mode modal detail (API mode navigates to /trials/:source/:id)
  const [selected, setSelected] = useState<Trial | null>(null);
  const [detailsCache, setDetailsCache] = useState<Record<string, Record<string, TrialDetailData>>>({});
  const [apiDetails, setApiDetails] = useState<Record<string, TrialDetailResponse>>({});
  const diseaseFile = index?.diseases.find((x) => x.key === profile)?.file;
  const detailsState = useJson<Record<string, TrialDetailData>>(
    !api && selected && diseaseFile && !detailsCache[profile]
      ? detailsUrl(dataSource, profile, diseaseFile) : null, isDetailsMap);
  useEffect(() => {
    if (!api && profile && detailsState.status === "ready") {
      setDetailsCache((prev) => (prev[profile] ? prev : { ...prev, [profile]: detailsState.data }));
    }
  }, [detailsState, profile, api]);
  const selectedKey = selected ? selected.source + ":" + selected.id : null;
  const apiDetailState = useJson<TrialDetailResponse>(
    api && selected && selectedKey && !apiDetails[selectedKey]
      ? `/api/trials/${encodeURIComponent(selected.source)}/${encodeURIComponent(selected.id)}` : null,
    isTrialDetailResponse);
  useEffect(() => {
    if (api && selectedKey && apiDetailState.status === "ready") {
      setApiDetails((prev) => (prev[selectedKey] ? prev : { ...prev, [selectedKey]: apiDetailState.data }));
    }
  }, [apiDetailState, selectedKey, api]);

  const openTrial = (x: Trial) => {
    if (api) navigate(`/trials/${encodeURIComponent(x.source)}/${encodeURIComponent(x.id)}`);
    else setSelected(x);
  };

  const detailFor: TrialDetailData | null = selected
    ? api
      ? apiDetails[selected.source + ":" + selected.id] ?? null
      : detailsCache[profile]?.[selected.source + ":" + selected.id] ?? null
    : null;
  const changeHistory = api && selected ? apiDetails[selected.source + ":" + selected.id]?.changeHistory ?? null : null;

  // Active search takes over the All view with the backend-side discovery
  // experience; with the backend offline the browse view below still filters
  // locally so the user is never stranded on a dead page.
  const naturalText = route.query.get("ask");
  if (isSavedView && api) {
    return <main className="page-main"><SavedSearchesPage t={t} /></main>;
  }
  if (!isWatchedView && api && naturalText) {
    return <main className="page-main"><NaturalSearch text={naturalText} t={t} lang={lang} /></main>;
  }
  if (!isWatchedView && api) {
    return (
      <main className="page-main">
        <p className="form-hint">{staleSources.length ? t("search.delayedHint", { sources: staleSources.map((s) => s.full_name).join(", ") }) : t("search.localHint")} <button className="text-btn" type="button" onClick={() => navigate("/data-sources")}>{t("search.dataSourcesLink")}</button></p>
        <SearchResults t={t} lang={lang} state={search} events={events} />
      </main>
    );
  }

  return (
    <main className="page-main">
      <div className="page-head">
        <div>
          <h1>{isWatchedView ? t("nav.watched") : t("nav.trials")}</h1>
          <p className="page-sub">
            {isWatchedView ? `${watchedRows?.length ?? 0} ${lang === "zh" ? "项关注的试验" : "watched trials"}` : index && t("trials.profileTotal", {
              name: lang === "zh" ? (index.diseases.find((x) => x.key === profile)?.label ?? "") : (index.diseases.find((x) => x.key === profile)?.label_en ?? ""),
              n: (data?.total ?? 0).toLocaleString(),
            })}
          </p>
        </div>
        {index && !isWatchedView && (
          <select className="disease-select" value={profile} aria-label={t("a11y.profile")}
                  onChange={(e) => { setIndexChoice(e.target.value); setSource("All"); setStatus("all"); setQuery(""); setVisible(60); }}>
            {index.diseases.map((x) => (
              <option key={x.key} value={x.key}>{lang === "zh" ? x.label : x.label_en} · {x.total.toLocaleString()}</option>
            ))}
          </select>
        )}
      </div>
      {api && <p className="form-hint">{staleSources.length ? t("search.delayedHint", { sources: staleSources.map((s) => s.full_name).join(", ") }) : t("search.localHint")} <button className="text-btn" type="button" onClick={() => navigate("/data-sources")}>{t("search.dataSourcesLink")}</button></p>}

      <div className="page-tabs" role="tablist" aria-label={t("a11y.trialViews")}>
        <button type="button" role="tab" aria-selected={!isWatchedView}
                className={`ptab ${!isWatchedView ? "ptab-on" : ""}`}
                onClick={() => navigate("/trials")}>{t("trials.allTab")}</button>
        <button type="button" role="tab" aria-selected={isWatchedView}
                className={`ptab ${isWatchedView ? "ptab-on" : ""}`}
                onClick={() => api ? navigate("/trials?view=watched") : navigate("/trials")}>
          {t("nav.watched")}
        </button>
        {api && <button type="button" role="tab" aria-selected={false}
          className="ptab" onClick={() => navigate("/trials?view=saved")}>{t("saved.title")}</button>}
      </div>

      {isWatchedView && api ? (
        <WatchedPane t={t} rows={watchedRows} error={watchedErr} onUnwatch={(registry, id) => unwatchTrial(registry, id).then(() => { setWatchTick((n) => n + 1); })} />
      ) : isWatchedView && !api ? (
        <EmptyState title={t("service.title")} hint={t("service.body")} />
      ) : (
        <>
          <div className="toolbar toolbar-static">
            <div className="search-row">
              <input className="search" placeholder={t("search.placeholder")} value={query}
                     aria-label={t("search.placeholder")}
                     onChange={(e) => { setQuery(e.target.value); setVisible(60); }}
                     onKeyDown={(e) => {
                       // Enter promotes the local filter to a real search URL
                       if (e.key === "Enter" && api && query.trim()) {
                         navigate(`/trials?${searchStateToQuery({ ...emptySearchState(), q: query.trim() })}`);
                       }
                     }} />
            </div>
            <div className="chips">
              <button type="button" className={`chip ${source === "All" ? "chip-on" : ""}`} onClick={() => { setSource("All"); setVisible(60); }}>
                {t("chip.all")} · {(data?.total ?? 0).toLocaleString()}
              </button>
              {[...sourceCounts.entries()].map(([src, n]) => (
                <button type="button" key={src}
                        className={`chip ${source === src ? "chip-on" : ""}`}
                        style={source === src ? { borderColor: SOURCE_BADGES[src]?.color, color: SOURCE_BADGES[src]?.color } : undefined}
                        onClick={() => { setSource(src); setVisible(60); }}>
                  {SOURCE_BADGES[src]?.label ?? src} · {n.toLocaleString()}
                </button>
              ))}
            </div>
            <div className="selects">
              <select value={status} onChange={(e) => { setStatus(e.target.value); setVisible(60); }} aria-label={t("status.all")}>
                <option value="all">{t("status.all")}</option>
                <option value="recruiting">{t("status.recruiting")}</option>
                <option value="completed">{t("status.completed")}</option>
                <option value="terminated">{t("status.terminated")}</option>
                <option value="other">{t("status.other")}</option>
              </select>
              <select value={sort} onChange={(e) => setSort(toSortKey(e.target.value))} aria-label={t("sort.lastUpdated")}>
                <option value="lastUpdated">{t("sort.lastUpdated")}</option>
                <option value="registration">{t("sort.registration")}</option>
                <option value="enrollment">{t("sort.enrollment")}</option>
                <option value="id">{t("sort.id")}</option>
              </select>
            </div>
          </div>

          <section className="grid" aria-label={t("a11y.panel.trials")}>
            {reportState.status === "error" && <div className="empty">{t("error.trials")}{reportState.message}</div>}
            {reportState.status === "loading" && <Loading label={t("loading.trials")} />}
            {shown.map((trial) => {
              const badge = SOURCE_BADGES[trial.source] ?? { label: trial.source, color: "#64748b" };
              const watched = watchedIds.has(trial.id);
              return (
                <article className="card card-click" key={trial.source + trial.id}
                         role="button" tabIndex={0}
                         aria-label={t("a11y.openTrial", { title: trial.title })}
                         onClick={() => openTrial(trial)}
                         onKeyDown={(e) => {
                           if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrial(trial); }
                         }}>
                  <div className="card-top">
                    <span className="src-badge" style={{ backgroundColor: badge.color }}>{badge.label}</span>
                    <span className={`pill ${statusTone(trial.status)}`}>{trial.status}</span>
                    {recentFlags.added.has(trial.id) && <span className="flag flag-new" title={t("badge.new")}>{t("badge.new")}</span>}
                    {!recentFlags.added.has(trial.id) && recentFlags.changed.has(trial.id) && (
                      <span className="flag flag-changed" title={t("badge.changed")}>{t("badge.changed")}</span>
                    )}
                  </div>
                  <h3 className="card-title" title={trial.title}>{trial.title}</h3>
                  {trial.scientificTitle && <p className="card-sci">{trial.scientificTitle}</p>}
                  <div className="meta">
                    <span>{phaseLabel(trial.phase, t)}</span>
                    <span>{trial.studyType ?? "—"}</span>
                    <span>{t("card.enrolled", { n: fmtEnrollment(trial.enrollment) })}</span>
                  </div>
                  {trial.conditions.length > 0 && (
                    <div className="tags">
                      {trial.conditions.slice(0, 3).map((c, i) => (
                        <span className="tag" key={i}>{c.length > 40 ? c.slice(0, 40) + "…" : c}</span>
                      ))}
                      {trial.conditions.length > 3 && <span className="tag tag-more">+{trial.conditions.length - 3}</span>}
                    </div>
                  )}
                  <div className="card-foot">
                    <span className="tid">{trial.id}</span>
                    <span className="card-foot-actions">
                      {api && (
                        <button type="button" className="details-hint" aria-pressed={watched}
                                onClick={(e) => { e.stopPropagation(); toggleWatch(trial); }}>
                          {watched ? t("trial.watching") : t("trial.watch")}
                        </button>
                      )}
                      <span className="details-hint">{t("card.details")}</span>
                    </span>
                  </div>
                </article>
              );
            })}
            {reportState.status !== "loading" && shown.length === 0 && (
              <EmptyState title={t("empty.noMatch")} />
            )}
          </section>

          {visible < filtered.length && (
            <div className="more-wrap">
              <button type="button" className="more" onClick={() => setVisible(visible + 120)}>
                {t("more.load", { n: (filtered.length - visible).toLocaleString() })}
              </button>
            </div>
          )}

          <footer className="foot">
            {t("foot.trials", {
              shown: filtered.length.toLocaleString(),
              total: (data?.total ?? 0).toLocaleString(),
              date: data ? asOfText(data) : "",
            })}
          </footer>
        </>
      )}

      {selected && (
        <TrialDetailModal
          trial={selected}
          detail={detailFor}
          changeHistory={changeHistory}
          liveCheckUrl={api && /^NCT\d+$/.test(selected.id) ? `/api/nct/study/${encodeURIComponent(selected.id)}` : null}
          detailState={detailFor ? "ready" : "loading"}
          onClose={() => setSelected(null)}
          watching={watchedIds.has(selected.id)}
          onToggleWatch={api ? () => toggleWatch(selected) : undefined}
          t={t}
        />
      )}
    </main>
  );
}

/** Watched trials view — persisted watches with unseen change state. */
function WatchedPane({ t, rows, error, onUnwatch }: {
  t: Translate; rows: WatchedTrial[] | null; error: string | null;
  onUnwatch: (registry: string, id: string) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Map<string, Trial>>(new Map());
  const [previewTrial, setPreviewTrial] = useState<Trial | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [mutationError, setMutationError] = useState("");
  const [busyKey, setBusyKey] = useState("");
  const previewTriggerRef = useRef<HTMLElement | null>(null);
  const asTrial = (watch: WatchedTrial): Trial => ({
    id: watch.trial_id, source: watch.registries[0] || "NCT", title: watch.title,
    status: watch.current_status || t("common.unknown"), conditions: [], sponsors: [], countries: [],
    secondaryEndpoints: [], interventions: [], locations: [], lastUpdated: watch.last_changed_at,
  });
  const select = (trial: Trial) => setSelected((current) => toggleTrialSelection(current, trial).selection);
  const removeWatch = (trial: Trial) => {
    const key = trialSelectionKey(trial); setBusyKey(key); setMutationError("");
    onUnwatch(trial.source, trial.id).catch(() => setMutationError(t("watched.unwatchFailed"))).finally(() => setBusyKey(""));
  };
  if (error) return <EmptyState title={t("watched.loadFailed")} hint={error} />;
  if (!rows) return <Loading label={t("loading.watched")} />;
  if (rows.length === 0) {
    return <EmptyState title={t("watched.empty")} hint={t("watched.emptyHint")}>
      <button type="button" className="more" onClick={() => navigate("/trials")}>{t("watched.browse")}</button>
    </EmptyState>;
  }
  const trials = rows.map(asTrial);
  const currentKeys = new Set(trials.map(trialSelectionKey));
  return (
    <>
      {mutationError && <p className="error-note" role="alert">{mutationError}</p>}
      <div className="trial-list trial-list-compact watched-trial-list" role="list">
        {rows.map((watch) => {
          const trial = asTrial(watch);
          return <TrialRow key={trialSelectionKey(trial)} trial={trial} t={t} density="compact" watched
            selected={selected.has(trialSelectionKey(trial))} watchBusy={busyKey === trialSelectionKey(trial)}
            watchMeta={{ unseen: watch.unseen_change_count, severity: watch.highest_unseen_severity, lastChanged: watch.last_changed_at }}
            onSelect={select} onWatch={removeWatch}
            onPreview={(value, trigger) => { previewTriggerRef.current = trigger; setPreviewTrial(value); }} />;
        })}
      </div>
      <TrialCompareBar trials={[...selected.values()]} currentKeys={currentKeys} t={t}
        onRemove={select} onClear={() => setSelected(new Map())} onCompare={() => setCompareOpen(true)} />
      {compareOpen && <TrialComparison trials={[...selected.values()]} t={t} onClose={() => setCompareOpen(false)}
        onRemove={(trial) => { select(trial); if (selected.size <= 2) setCompareOpen(false); }} />}
      <TrialPreviewDrawer trial={previewTrial} t={t} triggerRef={previewTriggerRef} watched selected={previewTrial ? selected.has(trialSelectionKey(previewTrial)) : false}
        onClose={() => setPreviewTrial(null)} onWatch={removeWatch} onSelect={select} />
    </>
  );
}
