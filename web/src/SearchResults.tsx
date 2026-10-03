/**
 * Search results (Phase 3E.1) — the read-only cross-registry discovery view
 * mounted at /trials?q=…&registries=…&statuses=…
 *
 * Architecture: the view is a pure function of the URL-encoded SearchState
 * (./searchState).  Every refinement re-navigates, so reload/share keep the
 * full query+filter state, and "Track this search" converts the SAME state
 * into a monitor rule through the one canonical conversion path — search is
 * an unsaved monitor preview, and it never writes monitor state itself.
 */

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { refreshableUrl, useJson, useStickyJson } from "./api";
import type { EventsData, LiveCheckResponse, Trial, TrialDetailFull, TrialSearchData, WatchedTrial } from "./types";
import { isTrialSearchData } from "./guards";
import {
  createMonitor, isWatchList, liveCheckTrials, previewMonitor, unwatchTrial,
  watchTrial,
} from "./monitoringApi";
import { SOURCE_BADGES, phaseLabel, statusTone } from "./labels";
import { Drawer, EmptyState, Skeleton } from "./ui";
import type { Lang, Translate } from "./i18n";
import { asOfText } from "./datasource";
import { navigate, useRoute } from "./router";
import { createSavedSearch, isSavedSearchResponse, updateSavedSearch } from "./savedSearchApi";
import {
  DEFAULT_PAGE_SIZE, SEARCH_SORTS, monitorRuleToSearchState,
  searchApiParams, searchStateToMonitorRule,
  searchStateToQuery, searchStateToSavedState, savedStateToSearchState, suggestedMonitorName,
  type SearchSort, type SearchState,
} from "./searchState";
import TrialRow from "./TrialRow";
import TrialFilters, { structuredFilterCount } from "./TrialFilters";
import TrialPreviewDrawer from "./TrialPreviewDrawer";
import TrialCompareBar from "./TrialCompareBar";
import {
  clearStructuredFilters, cloneFilterDraft, parseTrialDensity,
  toggleTrialSelection, trialSelectionKey, type TrialDensity,
} from "./trialViewState";
import { loadTrialDetail } from "./trialDetailCache";
import { safeExternalUrl } from "./urlSafety";

/** UTC ms of a "YYYY-MM-DD HH:MM:SS" registry stamp (NaN when absent). */
function stampMs(s: string | null | undefined): number {
  if (!s) return Number.NaN;
  return Date.parse(s.slice(0, 10) + "T" + (s.slice(11, 19) || "00:00:00") + "Z");
}

/** live-check source key → local registry short name (routes / SOURCE_BADGES). */
const LIVE_SOURCE_KEY: Record<string, string> = {
  chictr: "ChiCTR", ctr: "CTR", nct: "NCT", ictrp: "ICTRP",
};

/** live-check field_used → i18n key（本源本次检索用的原站字段）。 */
const LIVE_FIELD_LABEL: Record<string, "search.live.fieldTitle" | "search.live.fieldSponsor" | "search.live.fieldGeneral" | "search.live.fieldIndication"> = {
  title: "search.live.fieldTitle",
  secsponsor: "search.live.fieldSponsor",
  keywords: "search.live.fieldGeneral",
  indication: "search.live.fieldIndication",
};

/** Server-side source order (server/app.py live-check). */
const LIVE_SOURCE_ORDER = ["chictr", "ctr", "nct", "ictrp"] as const;

const trialKey = trialSelectionKey;

/** WHO primary-registry jurisdiction codes returned by the live adapter. */
const REGISTRY_JURISDICTIONS: Record<string, { en: string; zh: string }> = {
  US: { en: "United States", zh: "美国" }, CN: { en: "China", zh: "中国" },
  IN: { en: "India", zh: "印度" }, AU_NZ: { en: "Australia / New Zealand", zh: "澳大利亚/新西兰" },
  GB: { en: "United Kingdom", zh: "英国" }, DE: { en: "Germany", zh: "德国" },
  JP: { en: "Japan", zh: "日本" }, KR: { en: "South Korea", zh: "韩国" },
  NL: { en: "Netherlands", zh: "荷兰" }, BR: { en: "Brazil", zh: "巴西" },
  AFRICA: { en: "Africa region", zh: "非洲地区" }, LK: { en: "Sri Lanka", zh: "斯里兰卡" },
  IR: { en: "Iran", zh: "伊朗" }, TH: { en: "Thailand", zh: "泰国" },
  CU: { en: "Cuba", zh: "古巴" }, LB: { en: "Lebanon", zh: "黎巴嫩" },
  PE: { en: "Peru", zh: "秘鲁" }, EU: { en: "European Union", zh: "欧盟" },
};

export default function SearchResults({ t, lang, state, events }: {
  t: Translate; lang: Lang; state: SearchState; events: EventsData | null;
}) {
  const route = useRoute();
  const savedId = Number(route.query.get("saved_search")) || null;
  const [savedTick, setSavedTick] = useState(0);
  const savedResponse = useJson(savedId ? `/api/saved-searches/${savedId}?refresh=${savedTick}` : null, isSavedSearchResponse);
  const saved = savedResponse.status === "ready" ? savedResponse.data.saved_search : null;
  const original = saved?.original_nl_query ?? route.query.get("nl");
  const interpreterVersion = saved?.interpreter_version ?? route.query.get("iv");
  const [saving, setSaving] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedNotice, setSavedNotice] = useState("");
  const savedDiffers = saved ? searchStateToQuery({ ...state, page: 1 }) !==
    searchStateToQuery(savedStateToSearchState(saved.state)) : false;
  const apiUrl = "/api/trials/search?" + searchApiParams(state);
  const searchState = useJson<TrialSearchData>(apiUrl, isTrialSearchData);
  const data = searchState.status === "ready" ? searchState.data : null;

  // watched ids for inline watch toggles + batch watch — one cached GET
  // shared with the Trials page (instant on revisit, silent revalidate);
  // toggles bump the tick and the sticky hook holds the ids meanwhile
  const [watchTick, setWatchTick] = useState(0);
  const watches = useStickyJson<{ generated_at: string; watches: WatchedTrial[] }>(
    refreshableUrl("/api/watches", watchTick), isWatchList);
  const watchedIds = useMemo(
    () => new Set((watches.data?.watches ?? []).map((w) => w.trial_id)),
    [watches.data],
  );

  const toggleWatch = (trial: Trial) => {
    const watching = watchedIds.has(trial.id);
    const call = watching ? unwatchTrial(trial.source, trial.id) : watchTrial(trial.source, trial.id);
    call.then(() => setWatchTick((n) => n + 1)).catch(() => undefined);
  };

  // NEW/CHANGED flags (7d window of the global event stream)
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

  const apply = (next: SearchState) => {
    const p = new URLSearchParams(searchStateToQuery(next));
    if (savedId) p.set("saved_search", String(savedId));
    if (!savedId && original) p.set("nl", original);
    if (!savedId && interpreterVersion) p.set("iv", interpreterVersion);
    navigate(`/trials?${p}`);
  };
  const patch = (part: Partial<SearchState>) => apply({ ...state, page: 1, ...part });

  const [tracking, setTracking] = useState(false);
  useEffect(() => { if (route.query.get("track") === "1") setTracking(true); }, [route.url]);

  const updateStored = () => {
    if (!savedId) return;
    setUpdating(true); setSaveError("");
    updateSavedSearch(savedId, { state: searchStateToSavedState(state) })
      .then(() => { setSavedTick((x) => x + 1); setSavedNotice(t("saved.updatedNotice")); })
      .catch((e: unknown) => setSaveError(e instanceof Error ? e.message : String(e)))
      .finally(() => setUpdating(false));
  };

  const toggleIn = (list: string[], value: string): string[] =>
    list.includes(value) ? list.filter((x) => x !== value) : [...list, value];

  // Applied-filters summary (ClinicalTrials.gov-style): every active rule
  // dimension becomes one removable chip above the results.
  const selectedFilters: Array<{ dimension: string; value: string; remove: () => void }> = [];
  for (const v of state.registries) selectedFilters.push({ dimension: t("search.rule.registries"), value: SOURCE_BADGES[v]?.label ?? v, remove: () => patch({ registries: toggleIn(state.registries, v) }) });
  for (const v of state.statuses) selectedFilters.push({ dimension: t("search.rule.statuses"), value: v, remove: () => patch({ statuses: toggleIn(state.statuses, v) }) });
  for (const v of state.phase) selectedFilters.push({ dimension: t("search.rule.phase"), value: phaseLabel(v, t), remove: () => patch({ phase: toggleIn(state.phase, v) }) });
  for (const v of state.studyTypes) selectedFilters.push({ dimension: t("search.rule.study_types"), value: v, remove: () => patch({ studyTypes: toggleIn(state.studyTypes, v) }) });
  for (const v of state.country) selectedFilters.push({ dimension: t("search.rule.country"), value: v, remove: () => patch({ country: toggleIn(state.country, v) }) });
  for (const v of state.condition) selectedFilters.push({ dimension: t("search.rule.condition"), value: v, remove: () => patch({ condition: toggleIn(state.condition, v) }) });
  for (const v of state.intervention) selectedFilters.push({ dimension: t("search.rule.intervention"), value: v, remove: () => patch({ intervention: toggleIn(state.intervention, v) }) });
  for (const v of state.sponsor) selectedFilters.push({ dimension: t("search.rule.sponsor"), value: v, remove: () => patch({ sponsor: toggleIn(state.sponsor, v) }) });
  const clearAllFilters = () => apply(clearStructuredFilters(state));

  // CSV export of the current result set (client-side, paged via the same
  // search API; hard cap 20 × 100 rows so a runaway query cannot hog tabs).
  const [exporting, setExporting] = useState(false);
  const exportCsv = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      const rows: string[][] = [];
      let collected = 0;
      for (let p = 1; p <= 20; p++) {
        const res = await fetch(`/api/trials/search?${searchApiParams({ ...state, page: p }, 100)}`);
        const json: unknown = await res.json();
        if (!res.ok || !isTrialSearchData(json)) throw new Error(`HTTP ${res.status}`);
        for (const tr of json.trials) {
          rows.push([tr.source, tr.id, tr.title, tr.status, tr.phase ?? "", tr.studyType ?? "",
            tr.enrollment == null ? "" : String(tr.enrollment),
            tr.registrationDate ?? "", tr.startDate ?? "", tr.completionDate ?? "",
            tr.lastUpdated ?? "", tr.sponsors.join("; "), tr.conditions.join("; "),
            tr.countries.join("; "), tr.url ?? ""]);
        }
        collected += json.trials.length;
        if (json.trials.length === 0 || collected >= json.total) break;
      }
      const head = ["registry", "trial_id", "title", "status", "phase", "study_type", "enrollment",
        "registration_date", "start_date", "completion_date", "last_updated", "sponsors",
        "conditions", "countries", "url"];
      const cell = (v: string) => (/[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
      const csv = "\uFEFF" + [head, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      const stem = (state.q || "search").replace(/[^\w\u4e00-\u9fff]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "search";
      a.href = URL.createObjectURL(blob);
      a.download = `trials-${stem}-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch {
      setSaveError(t("search.exportFailed"));
    } finally {
      setExporting(false);
    }
  };

  // Comparison selection is intentionally independent from the URL so it can
  // survive pagination and refinements without polluting shareable searches.
  const [selected, setSelected] = useState<Map<string, Trial>>(new Map());
  const [compareOpen, setCompareOpen] = useState(false);
  const [selectionNotice, setSelectionNotice] = useState("");
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("ct-trial-compare-add");
      sessionStorage.removeItem("ct-trial-compare-add");
      if (!raw) return;
      const trial = JSON.parse(raw) as Trial;
      if (trial && typeof trial.source === "string" && typeof trial.id === "string" && typeof trial.title === "string") {
        setSelected((current) => toggleTrialSelection(current, trial).selection);
      }
    } catch { /* corrupt optional session state is ignored */ }
  }, []);

  const toggleSelect = (trial: Trial) => {
    setSelected((prev) => {
      const next = toggleTrialSelection(prev, trial);
      setSelectionNotice(next.blocked ? t("compare.maxReached") : t("compare.selectionUpdated", { n: next.selection.size }));
      return next.selection;
    });
  };
  const [batchBusy, setBatchBusy] = useState(false);
  const watchSelected = () => {
    const targets = [...selected.values()].filter((x) => !watchedIds.has(x.id));
    if (targets.length === 0) return;
    setBatchBusy(true);
    Promise.allSettled(targets.map((x) => watchTrial(x.source, x.id)))
      .then(() => setWatchTick((n) => n + 1))
      .finally(() => { setBatchBusy(false); });
  };

  const [density, setDensity] = useState<TrialDensity>(() => {
    try { return parseTrialDensity(localStorage.getItem("ct-trial-density")); } catch { return "compact"; }
  });
  const chooseDensity = (value: TrialDensity) => {
    setDensity(value);
    try { localStorage.setItem("ct-trial-density", value); } catch { /* storage may be unavailable */ }
  };
  const [withinQuery, setWithinQuery] = useState(state.q);
  useEffect(() => setWithinQuery(state.q), [state.q]);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersButtonRef = useRef<HTMLButtonElement>(null);
  const [filterDraft, setFilterDraft] = useState<SearchState>(() => cloneFilterDraft(state));
  const openFilters = () => { setFilterDraft(cloneFilterDraft(state)); setFiltersOpen(true); };
  const [previewTrial, setPreviewTrial] = useState<Trial | null>(null);
  const previewTriggerRef = useRef<HTMLElement | null>(null);
  const openPreview = (trial: Trial, trigger: HTMLButtonElement) => {
    previewTriggerRef.current = trigger; setPreviewTrial(trial);
  };
  const currentKeys = useMemo(() => new Set((data?.trials ?? []).map(trialKey)), [data]);
  const rangeStart = data && data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const rangeEnd = data ? Math.min(data.total, data.page * data.page_size) : 0;

  const totalPages = data ? Math.max(1, Math.ceil(data.total / Math.max(1, data.page_size || DEFAULT_PAGE_SIZE))) : 1;

  // ── 实时查原站（tier1 查漏回源按钮 / tier3 同步透传开关，共用端点） ──
  const [live, setLive] = useState<LiveCheckResponse | null>(null);
  const [liveBusy, setLiveBusy] = useState(false);
  const [liveError, setLiveError] = useState("");
  const [liveMode, setLiveMode] = useState(false);
  const liveBusyRef = useRef(false);
  // 逐源进度：每个源单独请求、完成即并入面板。服务端本就按源顺序串行
  // 抓取，拆成逐源请求只改变可见性（真实 x/4 进度），不改变原站节律。
  // liveRunRef 是批次代号：换词/关闭面板即作废在途批次，其余源不再发出。
  const [liveProgress, setLiveProgress] = useState<{
    done: number; total: number; current: string;
  } | null>(null);
  const liveRunRef = useRef(0);
  // 加载更多：与上次抓批强制间隔（WAF 节律；服务端同样强制）
  const [coolUntil, setCoolUntil] = useState(0);
  const [, setCoolTick] = useState(0);
  useEffect(() => {
    if (!coolUntil) return;
    const id = window.setInterval(() => setCoolTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [coolUntil]);
  const stopLiveBatch = () => {
    liveRunRef.current += 1;
    liveBusyRef.current = false;
    setLiveBusy(false);
    setLiveProgress(null);
  };
  useEffect(() => {
    stopLiveBatch();
    setLive(null);
    setLiveError("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.q]);
  const runLiveCheck = () => {
    const q = state.q.trim();
    if (!q || liveBusyRef.current) return;
    liveBusyRef.current = true;
    const run = ++liveRunRef.current;
    setLiveBusy(true);
    setLiveError("");
    setLive(null);
    const merged: LiveCheckResponse = { generated_at: "", q, results: [] };
    const publish = () => setLive({ ...merged, results: [...merged.results] });
    const replaceSource = (result: LiveCheckResponse) => {
      merged.generated_at = result.generated_at;
      const incoming = result.results[0];
      if (!incoming) return;
      const index = merged.results.findIndex((x) => x.source === incoming.source);
      if (index >= 0) merged.results[index] = incoming;
      else merged.results.push(incoming);
      publish();
    };
    const wait = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
    const drainSource = async (key: string, first: LiveCheckResponse): Promise<void> => {
      let current = first;
      replaceSource(current);
      while (liveRunRef.current === run && current.results[0]?.has_more) {
        // The server enforces the same interval. Waiting client-side avoids a
        // cooldown-only round trip and lets a manual source check finish all
        // pages without asking the user to keep clicking "Load more".
        if (key === "chictr" || key === "ctr") await wait(10_500);
        if (liveRunRef.current !== run) return;
        current = await liveCheckTrials(q, {
          continue: true, full_ingest: true, sources: [key],
        });
        const row = current.results[0];
        if (row?.status === "cooldown") {
          await wait(Math.max(row.retry_after ?? 1, 1) * 1000);
          continue;
        }
        replaceSource(current);
        if (!row || row.status === "error" || row.status === "circuit_open") return;
      }
    };
    const step = async (i: number): Promise<void> => {
      if (liveRunRef.current !== run || i >= LIVE_SOURCE_ORDER.length) {
        return;
      }
      const key = LIVE_SOURCE_ORDER[i];
      setLiveProgress({ done: i, total: LIVE_SOURCE_ORDER.length, current: key });
      try {
        const first = await liveCheckTrials(q, {
          sources: [key], full_ingest: true,
        });
        if (liveRunRef.current !== run) return;
        await drainSource(key, first);
      } catch (e: unknown) {
        if (liveRunRef.current !== run) return;
        // 单源网络失败不拖垮整批：以该源 error 行呈现，其余源继续
        replaceSource({ generated_at: "", q, results: [{
          source: key, status: "error", found: 0, queued: 0, enriched: 0,
          entries: [], error: e instanceof Error ? e.message : String(e),
        }] });
      }
      await step(i + 1);
    };
    step(0).finally(() => {
      if (liveRunRef.current !== run) return;
      liveBusyRef.current = false;
      setLiveBusy(false);
      setLiveProgress(null);
    });
  };
  const loadMoreLive = (source: string) => {
    const q = state.q.trim();
    if (!q || liveBusyRef.current) return;
    const run = liveRunRef.current;
    liveBusyRef.current = true;
    setLiveBusy(true);
    setCoolUntil(Date.now() + 12_000);
    liveCheckTrials(q, { continue: true, sources: [source] })
      .then((r) => {
        if (liveRunRef.current !== run) return;
        setLive((prev) => {
          if (!prev) return r;
          const fresh = new Map(r.results.map((x) => [x.source, x]));
          return { ...r, q: prev.q,
                   results: prev.results.map((x) => fresh.get(x.source) ?? x) };
        });
      })
      .catch((e: unknown) => {
        if (liveRunRef.current !== run) return;
        setLiveError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (liveRunRef.current !== run) return;
        liveBusyRef.current = false;
        setLiveBusy(false);
      });
  };
  useEffect(() => {
    if (!liveMode || !state.q.trim()) return;
    const id = window.setTimeout(runLiveCheck, 600);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveMode, state.q]);

  return (
    <>
      <div className="page-head search-head trials-page-head">
        <div>
          <h1 className="search-title">{t("nav.trials")}</h1>
          <p className="page-sub">
            {state.q ? t("trials.currentScope", { scope: state.q }) : t("trials.allScope")}
          </p>
          <p className="trials-source-summary">{t("trials.sourceSummary", { sources: scopeLine(data) })} · {data ? asOfText(data) : t("common.unknown")}
            {" · "}<button className="text-btn" type="button" onClick={() => navigate("/data-sources")}>{t("search.dataSourcesLink")}</button>
          </p>
        </div>
        <div className="search-actions">
          <button type="button" className="chip chip-on" onClick={() => setSaving(true)}>
            {saved ? t("saved.saveAs") : t("saved.save")}
          </button>
          {saved && savedDiffers && <button type="button" className="chip" disabled={updating} onClick={updateStored}>{t("saved.update")}</button>}
          <button type="button" className="chip chip-on" onClick={() => setTracking(true)}>
            {t("search.trackThisSearch")}
          </button>
          <button type="button" className={`chip ${liveMode ? "chip-on" : ""}`}
                  aria-pressed={liveMode} title={t("search.liveToggleHint")}
                  onClick={() => setLiveMode((v) => !v)}>
            {liveMode ? "◉ " : "◎ "}{t("search.liveToggle")}
          </button>
          <details className="action-menu">
            <summary className="chip">{t("search.moreActions")} ▾</summary>
            <div className="action-menu-pop">
              <button type="button" disabled={liveBusy || !state.q.trim()}
                      onClick={(e) => { e.currentTarget.closest("details")?.removeAttribute("open"); runLiveCheck(); }}>
                {liveBusy ? t("search.live.busy") : t("search.liveCheck")}
              </button>
              <button type="button" onClick={(e) => { e.currentTarget.closest("details")?.removeAttribute("open"); navigate("/trials?view=saved"); }}>
                {t("saved.title")}
              </button>
              <button type="button" onClick={(e) => {
                e.currentTarget.closest("details")?.removeAttribute("open");
                (navigator.clipboard?.writeText(`${window.location.origin}/trials?${searchStateToQuery(state)}`)
                  ?? Promise.reject(new Error("clipboard unavailable")))
                  .then(() => setSavedNotice(t("saved.linkCopied")))
                  .catch(() => setSaveError(t("saved.copyFailed")));
              }}>{t("saved.copyLink")}</button>
              <button type="button" disabled={exporting} title={t("search.exportHint")}
                      onClick={(e) => { e.currentTarget.closest("details")?.removeAttribute("open"); void exportCsv(); }}>
                {exporting ? t("search.exporting") : t("search.exportCsv")}
              </button>
              <button type="button" onClick={(e) => { e.currentTarget.closest("details")?.removeAttribute("open"); navigate("/trials"); }}>
                {t("search.clearSearch")}
              </button>
            </div>
          </details>
        </div>
      </div>

      <div className="page-tabs trials-view-tabs" role="tablist" aria-label={t("a11y.trialViews")}>
        <a role="tab" aria-selected="true" className="ptab ptab-on" href="/trials" onClick={(event) => { event.preventDefault(); navigate("/trials"); }}>{t("trials.allTab")}</a>
        <a role="tab" aria-selected="false" className="ptab" href="/trials?view=watched" onClick={(event) => { event.preventDefault(); navigate("/trials?view=watched"); }}>{t("nav.watched")}</a>
        <a role="tab" aria-selected="false" className="ptab" href="/trials?view=saved" onClick={(event) => { event.preventDefault(); navigate("/trials?view=saved"); }}>{t("saved.title")}</a>
      </div>

      <form className="trials-within-search" role="search" onSubmit={(event) => { event.preventDefault(); patch({ q: withinQuery.trim() }); }}>
        <label htmlFor="trials-within-query">{t("trials.withinSearch")}</label>
        <div><input id="trials-within-query" value={withinQuery} onChange={(event) => setWithinQuery(event.target.value)} placeholder={t("search.placeholder")} />
          <button type="submit" className="ui-button ui-button-primary">{t("trials.searchWithinAction")}</button></div>
      </form>

      {(live || liveError || liveBusy) && (
        <div className="live-panel" role="status">
          <div className="live-head">
            <span className="live-title">{t("search.live.title")}</span>
            <span className="live-q">“{state.q}”</span>
            {liveBusy && liveProgress && (
              <span className="live-progress">
                {t("search.live.progress", { done: liveProgress.done, total: liveProgress.total })}
                {" · "}{SOURCE_BADGES[LIVE_SOURCE_KEY[liveProgress.current]]?.label ?? liveProgress.current}
              </span>
            )}
            <button type="button" className="text-btn" aria-label={t("a11y.close")}
                    onClick={() => { stopLiveBatch(); setLive(null); setLiveError(""); }}>✕</button>
          </div>
          {liveBusy && (
            <div className="progress-track progress-thin" aria-hidden="true">
              <div className="progress-fill" />
            </div>
          )}
          {liveError && <p className="error-note" role="alert">{t("search.live.error")}: {liveError}</p>}
          {(live?.results ?? []).map((r) => (
            <div className="live-source" key={r.source}>
              <div className="live-source-head">
                <span className="src-badge" style={{ backgroundColor: SOURCE_BADGES[LIVE_SOURCE_KEY[r.source]]?.color ?? "#64748b" }}>
                  {SOURCE_BADGES[LIVE_SOURCE_KEY[r.source]]?.label ?? r.source}
                </span>
                {(r.status === "ok" || r.status === "cached") && <span className="live-stat">{t("search.live.found", { n: r.found })}</span>}
                {r.field_used && LIVE_FIELD_LABEL[r.field_used] && (
                  <span className="live-stat">{t(LIVE_FIELD_LABEL[r.field_used])}</span>
                )}
                {r.query_used && r.query_used !== state.q.trim() && (
                  <span className="live-stat">{t("search.live.queryUsed", { q: r.query_used })}</span>
                )}
                {r.status === "ok" && r.site_total != null && r.site_total > r.found && (
                  <span className="live-stat">{t("search.live.siteTotal", { n: r.site_total })}</span>
                )}
                {r.status === "ok" && r.queued > 0 && <span className="live-stat">{t("search.live.queued", { n: r.queued })}</span>}
                {r.status === "ok" && r.enriched > 0 && <span className="live-stat">{t("search.live.enriched", { n: r.enriched })}</span>}
                {r.status === "cached" && <span className="live-stat">{t("search.live.cached")}</span>}
                {r.status === "cooldown" && <span className="live-stat live-warn">{t("search.live.cooldown", { n: r.retry_after ?? 10 })}</span>}
                {r.status === "unsupported" && <span className="live-stat">{t("search.live.unsupported")}</span>}
                {r.status === "circuit_open" && <span className="live-stat live-warn">{t("search.live.circuit")}</span>}
                {r.status === "error" && <span className="live-stat live-warn">{t("search.live.error")}{r.error ? `: ${r.error}` : ""}</span>}
              </div>
              {r.source === "chictr" && r.field_used === "title" && (
                <p className="live-scope-note">{t("search.live.scopeTitle")}</p>
              )}
              {r.entries.length > 0 && (
                <ul className="live-entries">
                  {r.entries.slice(0, 20).map((e) => (
                    <li key={e.source_trial_id}>
                      {e.in_library ? (
                        <a className="live-link"
                           href={`/trials/${encodeURIComponent(LIVE_SOURCE_KEY[r.source] ?? r.source)}/${encodeURIComponent(e.source_trial_id)}`}>
                          {e.title || e.source_trial_id}
                        </a>
                      ) : e.url ? (
                        <a className="live-link" href={e.url} target="_blank" rel="noreferrer">
                          {e.title || e.source_trial_id}
                        </a>
                      ) : (
                        <span>{e.title || e.source_trial_id}</span>
                      )}
                      <span className="tid">{e.source_trial_id}</span>
                      {r.source === "ictrp" && e.source_registry && e.source_jurisdiction && (
                        <span className="live-origin" title={t("search.live.registryOriginHint")}>
                          {t("search.live.registryOrigin")}: {REGISTRY_JURISDICTIONS[e.source_jurisdiction]?.[lang] ?? e.source_jurisdiction}
                          {" · "}{e.source_registry}
                        </span>
                      )}
                      <span className={`live-flag ${e.in_library ? "live-flag-in" : "live-flag-pending"}`}>
                        {e.in_library ? t("search.live.inLibrary") : t("search.live.pending")}
                      </span>
                    </li>
                  ))}
                  {r.entries.length > 20 && <li className="live-more">{t("search.live.more", { n: r.entries.length - 20 })}</li>}
                </ul>
              )}
              {(r.status === "ok" || r.status === "cached") && r.has_more && (
                <button type="button" className="chip live-load-more"
                        disabled={liveBusy || Date.now() < coolUntil}
                        onClick={() => loadMoreLive(r.source)}>
                  {t("search.live.loadMore", { have: r.found })}
                </button>
              )}
            </div>
          ))}
          <p className="form-hint">{t("search.live.hint")}</p>
        </div>
      )}

      {saved && <p className="form-hint">{t("saved.openedAs", { name: saved.name })} · {savedDiffers ? t("saved.unsavedEdits") : t("saved.passive")}</p>}
      {original && <p className="form-hint">{t("saved.original")}: {original}</p>}
      {savedNotice && <p role="status">{savedNotice}</p>}
      {saveError && <p className="error-note" role="alert">{saveError}</p>}

      {selectedFilters.length > 0 && (
        <div className="sel-filters" role="group" aria-label={t("search.selectedFilters")}>
          <span className="sel-filters-label">{t("search.selectedFilters")}</span>
          {selectedFilters.map((f) => (
            <span className="sel-filter" key={f.dimension + f.value}>
              <small>{f.dimension}</small> {f.value}
              <button type="button" aria-label={t("search.removeFilter", { value: f.value })}
                      onClick={f.remove}>×</button>
            </span>
          ))}
          <button type="button" className="text-btn" onClick={clearAllFilters}>
            {t("search.clearAllFilters")}
          </button>
        </div>
      )}

      <div className="trials-result-toolbar" aria-label={t("trials.resultTools")}>
        <div className="trials-result-range">
          <strong>{data ? (data.total === 1 ? t("search.foundOne", { n: 1 }) : t("search.found", { n: data.total.toLocaleString() })) : t("search.searching")}</strong>
          {data && <span>{t("trials.showingRange", { start: rangeStart, end: rangeEnd })}</span>}
        </div>
        <button ref={filtersButtonRef} type="button" className="ui-button ui-button-secondary trials-filters-button" onClick={openFilters}>
          {t("search.filters")} · {structuredFilterCount(state)}
        </button>
        <label className="trials-sort">{t("search.sortLabel")}<select value={state.sort} onChange={(event) => patch({ sort: event.target.value as SearchSort })}>
          {SEARCH_SORTS.map((sort) => <option key={sort} value={sort}>{t(`search.sort.${sort}` as never)}</option>)}
        </select></label>
        <div className="trials-density" role="group" aria-label={t("trials.density")}>
          <button type="button" aria-pressed={density === "comfortable"} onClick={() => chooseDensity("comfortable")}>{t("trials.comfortable")}</button>
          <button type="button" aria-pressed={density === "compact"} onClick={() => chooseDensity("compact")}>{t("trials.compact")}</button>
        </div>
        <button type="button" className="ui-button ui-button-secondary" disabled={exporting} onClick={() => void exportCsv()}>{exporting ? t("search.exporting") : t("search.exportCsv")}</button>
        <span className="trials-selected-count">{t("search.selected", { n: selected.size })}</span>
      </div>

      <div className="trials-workspace">
        <aside className="trials-filter-rail" aria-label={t("search.filtersAria")}>
          <TrialFilters t={t} state={state} facets={data?.facets} onChange={apply} />
        </aside>
        <div className="trials-results" aria-busy={searchState.status === "loading" || undefined}>
          {searchState.status === "error" && <EmptyState title={t("error.trials")} hint={searchState.message}>
            <button type="button" className="ui-button ui-button-secondary" onClick={() => apply({ ...state })}>{t("service.retry")}</button>
          </EmptyState>}
          {searchState.status === "loading" && <div className="trial-list-skeleton"><Skeleton label={t("search.searching")} lines={4} /><Skeleton lines={4} /><Skeleton lines={4} /></div>}
          {searchState.status === "ready" && data && data.total === 0 && <EmptyState
            title={state.q ? t("search.noResults", { q: state.q }) : t("search.noResultsFilters")} hint={t("search.noResultsHint")}>
            {data.unfiltered_total != null && data.unfiltered_total > 0 && <button type="button" className="ui-button ui-button-primary"
              onClick={() => apply({ ...monitorRuleToSearchState({ query: state.q }), sort: state.sort })}>{t("search.clearFiltersKeepQuery", { n: data.unfiltered_total.toLocaleString() })}</button>}
            {structuredFilterCount(state) > 0 && <button type="button" className="ui-button ui-button-secondary" onClick={clearAllFilters}>{t("search.clearAllFilters")}</button>}
          </EmptyState>}
          {searchState.status === "ready" && data && data.total > 0 && <section className={`trial-list trial-list-${density}`} role="list" aria-label={t("a11y.panel.trials")}>
            {data.trials.map((trial) => <TrialRow key={trialKey(trial)} trial={trial} t={t} density={density}
              selected={selected.has(trialKey(trial))} watched={watchedIds.has(trial.id)}
              recent={recentFlags.added.has(trial.id) ? "new" : recentFlags.changed.has(trial.id) ? "changed" : undefined}
              onSelect={toggleSelect} onWatch={toggleWatch} onPreview={openPreview} />)}
          </section>}
        </div>
      </div>

      <p className="sr-only" aria-live="polite">{selectionNotice}</p>
      {selected.size === 1 && <div className="trial-selection-one" role="status">
        <span>{t("search.selected", { n: 1 })}</span><button type="button" className="text-btn" onClick={() => setSelected(new Map())}>{t("search.clearSelection")}</button>
        <button type="button" className="text-btn" disabled={batchBusy} onClick={watchSelected}>{t("search.watchSelected")}</button>
      </div>}
      <TrialCompareBar trials={[...selected.values()]} currentKeys={currentKeys} t={t}
        onRemove={toggleSelect} onClear={() => setSelected(new Map())} onCompare={() => setCompareOpen(true)} />
      {compareOpen && <TrialComparison trials={[...selected.values()]} t={t} onClose={() => setCompareOpen(false)}
        onRemove={(trial) => { toggleSelect(trial); if (selected.size <= 2) setCompareOpen(false); }} />}

      <Drawer open={filtersOpen} label={t("search.filters")} onClose={() => setFiltersOpen(false)} triggerRef={filtersButtonRef} mobile="bottom">
        <div className="trial-filter-drawer">
          <header><h2>{t("search.filters")}</h2><button type="button" className="ui-icon-button" aria-label={t("a11y.close")} onClick={() => setFiltersOpen(false)}>×</button></header>
          <TrialFilters t={t} state={filterDraft} facets={data?.facets} draft onChange={setFilterDraft} onClear={() => setFilterDraft(clearStructuredFilters(filterDraft))} />
          <footer><button type="button" className="ui-button ui-button-secondary" onClick={() => setFiltersOpen(false)}>{t("common.cancel")}</button>
            <button type="button" className="ui-button ui-button-primary" onClick={() => { apply(filterDraft); setFiltersOpen(false); }}>{t("search.applyFilters")}</button></footer>
        </div>
      </Drawer>

      <TrialPreviewDrawer trial={previewTrial} t={t} triggerRef={previewTriggerRef}
        watched={previewTrial ? watchedIds.has(previewTrial.id) : false}
        selected={previewTrial ? selected.has(trialKey(previewTrial)) : false}
        onClose={() => setPreviewTrial(null)} onWatch={toggleWatch} onSelect={toggleSelect} />

      {searchState.status === "ready" && data && data.total > 0 && (
        <div className="pager">
          <button type="button" className="chip" disabled={state.page <= 1}
                  onClick={() => apply({ ...state, page: state.page - 1 })}>
            ← {t("search.prevPage")}
          </button>
          <span className="page-info">
            {t("search.pageOf", { page: state.page, total: totalPages, n: data.total.toLocaleString() })}
          </span>
          <button type="button" className="chip" disabled={state.page >= totalPages}
                  onClick={() => apply({ ...state, page: state.page + 1 })}>
            {t("search.nextPage")} →
          </button>
        </div>
      )}

      {searchState.status === "ready" && data && (
        <footer className="foot">
          {t("search.dataFreshness", { date: asOfText(data) })}
        </footer>
      )}

      {tracking && (
        <TrackSearchModal state={state} total={data?.total ?? null} t={t}
                          onClose={() => setTracking(false)} />
      )}
      {saving && <SaveSearchModal state={state} total={data?.total ?? null} t={t}
        original={original} interpreterVersion={interpreterVersion} onClose={() => setSaving(false)} />}
    </>
  );
}

/** Registry scope line for the header — canonical labels, not raw ids. */
function scopeLine(data: TrialSearchData | null): string {
  const registries = data ? Object.keys(data.facets.registries) : [];
  if (registries.length === 0) return "ClinicalTrials.gov · ChiCTR · CTR · EU CTIS · ISRCTN · EUCTR · WHO ICTRP";
  return registries.map((r) => SOURCE_BADGES[r]?.label ?? r).join(" · ");
}

function SaveSearchModal({ state, total, t, original, interpreterVersion, onClose }: {
  state: SearchState; total: number | null; t: Translate;
  original: string | null; interpreterVersion: string | null; onClose: () => void;
}) {
  const [name, setName] = useState(suggestedMonitorName(state).slice(0, 80));
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const statePayload = searchStateToSavedState(state);
  const save = () => {
    setBusy(true); setError("");
    createSavedSearch({ name, description, state: statePayload,
      original_nl_query: original, interpreter_version: interpreterVersion })
      .then(({ saved_search }) => {
        onClose();
        const p = new URLSearchParams(searchStateToQuery({ ...state, page: 1 }));
        p.set("saved_search", String(saved_search.id));
        navigate(`/trials?${p}`);
      })
      .catch((e: unknown) => { setError(e instanceof Error ? e.message : String(e)); setBusy(false); });
  };
  return <div className="modal-backdrop" onClick={onClose}>
    <div className="modal track-modal" role="dialog" aria-modal="true" aria-label={t("saved.save")}
      onClick={(e) => e.stopPropagation()}>
      <h2>{t("saved.save")}</h2>
      <label className="form-row">{t("mon.name")}<input autoFocus value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="form-row">{t("saved.description")}<input value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      <h3>{t("search.ruleSummary")}</h3>
      <div className="rules-grid">{Object.entries(statePayload).filter(([k]) => k !== "sort").map(([key, values]) =>
        <div className="rule-item" key={key}><span className="rule-key">{t(`search.rule.${key === "q" ? "query" : key}` as never)}</span>
          <span className="rule-val">{Array.isArray(values) ? values.join(", ") : values}</span></div>)}</div>
      {original && <p>{t("saved.original")}: {original}</p>}
      <p>{total === null ? t("search.counting") : t("search.currentMatches", { n: total })} · {t("saved.currentSnapshot")}</p>
      <p className="form-hint">{t("saved.passive")}</p>
      {error && <p role="alert">{error}</p>}
      <button type="button" disabled={busy || !name.trim()} onClick={save}>{t("saved.save")}</button>
      <button type="button" disabled={busy} onClick={onClose}>{t("a11y.close")}</button>
    </div>
  </div>;
}

/**
 * Track-this-search confirmation (#20–#23): names the monitor, restates the
 * generated rule, previews the current match count via the monitor preview
 * API, and explains the watch-vs-monitor difference + quiet baseline before
 * anything is persisted.
 */
function TrackSearchModal({ state, total, t, onClose }: {
  state: SearchState; total: number | null; t: Translate; onClose: () => void;
}) {
  const [name, setName] = useState(suggestedMonitorName(state));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const rule = useMemo(() => searchStateToMonitorRule(state), [state]);
  const [previewCount, setPreviewCount] = useState<number | null>(null);
  useEffect(() => {
    let alive = true;
    previewMonitor(rule)
      .then((r) => { if (alive) setPreviewCount(r.matched_count); })
      .catch(() => { if (alive) setPreviewCount(null); });
    return () => { alive = false; };
  }, [rule]);

  const create = () => {
    setBusy(true);
    setErr("");
    createMonitor({ name: name.trim() || suggestedMonitorName(state), rules: rule })
      .then((r) => navigate(`/monitors/${r.id}`))
      .catch((e: unknown) => { setErr(e instanceof Error ? e.message : String(e)); setBusy(false); });
  };

  const ruleRows: Array<[string, string[]]> = (
    [
      ["query", rule.query], ["registries", rule.registries], ["statuses", rule.statuses],
      ["study_types", rule.study_types], ["phase", rule.phase], ["country", rule.country],
      ["condition", rule.condition], ["intervention", rule.intervention], ["sponsor", rule.sponsor],
    ] as Array<[string, string[] | undefined]>
  ).filter(([, values]) => values != null && values.length > 0) as Array<[string, string[]]>;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal track-modal" role="dialog" aria-modal="true"
           aria-labelledby="track-search-title" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="modal-head-left">
            <h2 id="track-search-title">{t("search.trackTitle")}</h2>
          </div>
          <div className="modal-head-right">
            <button type="button" className="control-btn" aria-label={t("a11y.close")}
                    onClick={onClose}>✕</button>
          </div>
        </div>
        <div className="modal-body">
          <label className="form-row">
            <span>{t("mon.name")}</span>
            <input className="search" value={name} aria-label={t("mon.name")}
                   onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="track-count" role="status">
            {previewCount === null
              ? t("search.counting")
              : t("search.currentMatches", { n: previewCount.toLocaleString() })}
            {previewCount !== null && total !== null && previewCount !== total && (
              <span className="form-hint"> ({t("search.previewDrift")})</span>
            )}
          </div>
          <h3 className="section-title">{t("search.ruleSummary")}</h3>
          <div className="rules-grid">
            {ruleRows.map(([key, values]) => (
              <div className="rule-item" key={key}>
                <span className="rule-key">{t(`search.rule.${key}` as never)}</span>
                <span className="rule-val">{values.join(", ")}</span>
              </div>
            ))}
            {ruleRows.length === 0 && <span className="form-hint">{t("search.noFilters")}</span>}
          </div>
          <p className="form-hint">{t("search.trackExplainer")}</p>
          <p className="form-hint">{t("search.baselineExplainer")}</p>
          {err && <div className="error-note" role="alert">{err}</div>}
          <div className="track-actions">
            <button type="button" className="chip chip-on" disabled={busy} onClick={create}>
              {t("search.createMonitor")}
            </button>
            <button type="button" className="chip" disabled={busy} onClick={onClose}>
              {t("a11y.close")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function TrialComparison({ trials, t, onClose, onRemove }: {
  trials: Trial[]; t: Translate; onClose: () => void; onRemove?: (trial: Trial) => void;
}) {
  type Row = {
    section: Parameters<Translate>[0];
    label: Parameters<Translate>[0];
    value: (trial: Trial, detail?: TrialDetailFull) => string;
    long?: boolean;
  };
  const signature = trials.map(trialKey).join("|");
  const [details, setDetails] = useState<Record<string, TrialDetailFull>>({});
  const [detailErrors, setDetailErrors] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState<Set<string>>(new Set());
  const [differencesOnly, setDifferencesOnly] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setDetails({});
    setDetailErrors(new Set());
    Promise.allSettled(trials.map(async (trial) => [trialKey(trial), await loadTrialDetail(trial.source, trial.id)] as const))
      .then((results) => {
        if (!alive) return;
        const next: Record<string, TrialDetailFull> = {};
        const failed = new Set<string>();
        results.forEach((result, index) => {
          const key = trialKey(trials[index]);
          if (result.status === "fulfilled") next[result.value[0]] = result.value[1];
          else failed.add(key);
        });
        setDetails(next);
        setDetailErrors(failed);
      })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // `signature` is the stable identity of the selected columns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  const fullTrial = (trial: Trial): Trial => details[trialKey(trial)]?.trial ?? trial;
  const list = (values: unknown): string => {
    if (!Array.isArray(values) || values.length === 0) return "—";
    return values.map((value) => typeof value === "string" ? value : JSON.stringify(value)).join("; ");
  };
  const rows: Row[] = [
    { section: "search.compare.section.core", label: "search.compare.registry", value: (trial) => SOURCE_BADGES[trial.source]?.label ?? trial.source },
    { section: "search.compare.section.core", label: "search.compare.status", value: (trial) => fullTrial(trial).status || "—" },
    { section: "search.compare.section.core", label: "search.compare.phase", value: (trial) => phaseLabel(fullTrial(trial).phase, t) },
    { section: "search.compare.section.core", label: "search.compare.studyType", value: (trial) => fullTrial(trial).studyType ?? "—" },
    { section: "search.compare.section.core", label: "search.compare.enrollment", value: (trial) => fullTrial(trial).enrollment?.toLocaleString() ?? "—" },
    { section: "search.compare.section.core", label: "search.compare.scientificTitle", value: (trial) => fullTrial(trial).scientificTitle ?? "—", long: true },
    { section: "search.compare.section.timeline", label: "search.compare.registration", value: (trial) => fullTrial(trial).registrationDate ?? "—" },
    { section: "search.compare.section.timeline", label: "search.compare.start", value: (trial) => fullTrial(trial).startDate ?? "—" },
    { section: "search.compare.section.timeline", label: "search.compare.completion", value: (trial) => fullTrial(trial).completionDate ?? "—" },
    { section: "search.compare.section.timeline", label: "search.compare.updated", value: (trial) => fullTrial(trial).lastUpdated ?? "—" },
    { section: "search.compare.section.timeline", label: "search.compare.inLibrary", value: (_trial, detail) => detail?.first_crawled_at ?? "—" },
    { section: "search.compare.section.timeline", label: "search.compare.changes", value: (_trial, detail) => detail?.changeHistory ? detail.changeHistory.length.toLocaleString() : "—" },
    { section: "search.compare.section.design", label: "search.compare.summary", value: (_trial, detail) => detail?.summary ?? "—", long: true },
    { section: "search.compare.section.design", label: "search.compare.investigator", value: (_trial, detail) => detail?.investigator ?? "—" },
    { section: "search.compare.section.design", label: "search.compare.sponsor", value: (trial) => list(fullTrial(trial).sponsors) },
    { section: "search.compare.section.design", label: "search.compare.conditions", value: (trial) => list(fullTrial(trial).conditions) },
    { section: "search.compare.section.outcomes", label: "search.compare.interventions", value: (trial) => list(fullTrial(trial).interventions), long: true },
    { section: "search.compare.section.outcomes", label: "search.compare.primaryEndpoint", value: (trial) => fullTrial(trial).primaryEndpoint ?? "—", long: true },
    { section: "search.compare.section.outcomes", label: "search.compare.secondaryEndpoints", value: (trial) => list(fullTrial(trial).secondaryEndpoints), long: true },
    { section: "search.compare.section.eligibility", label: "search.compare.inclusion", value: (_trial, detail) => detail?.inclusion ?? "—", long: true },
    { section: "search.compare.section.eligibility", label: "search.compare.exclusion", value: (_trial, detail) => detail?.exclusion ?? "—", long: true },
    { section: "search.compare.section.footprint", label: "search.compare.countries", value: (trial) => list(fullTrial(trial).countries) },
    { section: "search.compare.section.footprint", label: "search.compare.locations", value: (trial) => list(fullTrial(trial).locations), long: true },
  ];
  const valuesFor = (row: Row) => trials.map((trial) => row.value(trial, details[trialKey(trial)]));
  const normalized = (value: string) => value.replace(/\s+/g, " ").trim().toLocaleLowerCase();
  const differs = (row: Row) => new Set(valuesFor(row).map(normalized)).size > 1;
  const visibleRows = rows.filter((row) => !differencesOnly || differs(row));
  const differenceCount = rows.filter(differs).length;
  const sections = [...new Set(visibleRows.map((row) => row.section))];
  const retryDetail = (trial: Trial) => {
    const key = trialKey(trial);
    setRetrying((current) => new Set(current).add(key));
    loadTrialDetail(trial.source, trial.id, true).then((value) => {
      setDetails((current) => ({ ...current, [key]: value }));
      setDetailErrors((current) => { const next = new Set(current); next.delete(key); return next; });
    }).catch(() => setDetailErrors((current) => new Set(current).add(key)))
      .finally(() => setRetrying((current) => { const next = new Set(current); next.delete(key); return next; }));
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal comparison-modal" role="dialog" aria-modal="true"
           aria-labelledby="trial-compare-title" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div className="modal-head-left">
            <h2 id="trial-compare-title">{t("search.compare.title")}</h2>
            <p className="form-hint">
              {t("search.compare.summaryLine", { differences: differenceCount, fields: rows.length })}
              {loading && <> · {t("search.compare.loading")}</>}
            </p>
          </div>
          <button type="button" className="control-btn" aria-label={t("a11y.close")} onClick={onClose}>✕</button>
        </div>
        <div className="comparison-toolbar">
          <label className="compare-toggle">
            <input type="checkbox" checked={differencesOnly}
                   onChange={(event) => setDifferencesOnly(event.target.checked)} />
            {t("search.compare.differencesOnly")}
          </label>
          <span className="form-hint">{t("search.compare.hint")}</span>
        </div>
        <div className="comparison-cards">
          {trials.map((trial) => {
            const resolved = fullTrial(trial);
            const badge = SOURCE_BADGES[trial.source] ?? { label: trial.source, color: "#64748b" };
            return <article className="comparison-card" key={trialKey(trial)}>
              <div className="comparison-card-top">
                <span className="src-badge" style={{ backgroundColor: badge.color }}>{badge.label}</span>
                <span className={`pill ${statusTone(resolved.status)}`}>{resolved.status}</span>
              </div>
              <h3>{resolved.title}</h3>
              <span className="tid">{resolved.id}</span>
              <div className="comparison-card-metrics">
                <span><small>{t("search.compare.phase")}</small>{phaseLabel(resolved.phase, t)}</span>
                <span><small>{t("search.compare.enrollment")}</small>{resolved.enrollment?.toLocaleString() ?? "—"}</span>
                <span><small>{t("search.compare.updated")}</small>{resolved.lastUpdated ?? "—"}</span>
              </div>
              {detailErrors.has(trialKey(trial)) && <p className="error-note">{t("search.compare.detailError")} <button type="button" className="text-btn" disabled={retrying.has(trialKey(trial))} onClick={() => retryDetail(trial)}>{t("service.retry")}</button></p>}
              <div className="comparison-card-actions">
                <button type="button" className="text-btn"
                        onClick={() => navigate(`/trials/${encodeURIComponent(trial.source)}/${encodeURIComponent(trial.id)}`)}>
                  {t("search.compare.openDetail")}
                </button>
                {safeExternalUrl(resolved.url) && <a className="text-btn" href={safeExternalUrl(resolved.url)!} target="_blank" rel="noreferrer">
                  {t("search.compare.openRegistry")}
                </a>}
                {onRemove && <button type="button" className="text-btn" onClick={() => onRemove(trial)}>{t("preview.removeCompare")}</button>}
              </div>
            </article>;
          })}
        </div>
        <div className="comparison-scroll">
          <table className="comparison-table">
            <thead><tr><th>{t("search.compare.field")}</th>{trials.map((trial) => (
              <th key={trialKey(trial)}>
                <button type="button" className="text-btn"
                        onClick={() => navigate(`/trials/${encodeURIComponent(trial.source)}/${encodeURIComponent(trial.id)}`)}>
                  {trial.title}
                </button>
                <span className="tid">{trial.id}</span>
              </th>
            ))}</tr></thead>
            <tbody>{sections.map((section) => <Fragment key={section}>
              <tr className="comparison-section"><th colSpan={trials.length + 1}>{t(section)}</th></tr>
              {visibleRows.filter((row) => row.section === section).map((row) => {
                const rowDiffers = differs(row);
                const values = valuesFor(row);
                return <tr key={row.label} className={rowDiffers ? "comparison-different" : ""}>
                  <th>{t(row.label)}{rowDiffers && <span className="difference-dot" title={t("search.compare.differs")}>●</span>}</th>
                  {trials.map((trial, index) => <td key={trialKey(trial)}>
                    <ComparisonValue value={values[index]} long={row.long} t={t} />
                  </td>)}
                </tr>;
              })}
            </Fragment>)}</tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function ComparisonValue({ value, long, t }: { value: string; long?: boolean; t: Translate }) {
  if (value === "—") return <span className="comparison-missing">{t("search.compare.missing")}</span>;
  if (!long || value.length <= 180) return <span>{value}</span>;
  return <details className="comparison-long">
    <summary>{value.slice(0, 170).trim()}… <span>{t("search.compare.expand")}</span></summary>
    <div>{value}</div>
  </details>;
}
