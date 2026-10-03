/**
 * Trial detail page (Phase 3E) — a routed product surface replacing the
 * long single-page dump:
 *
 *   /trials/:source/:id?tab=overview|changes|history|sources
 *
 *  - compact header (registry badge, id, title, status/phase/type/sponsor,
 *    last updated, watch state, recorded change count);
 *  - Overview: current state only, grouped semantically, sites as a real
 *    table (never JSON), right rail with status/last change/monitorships;
 *  - Changes: persisted event history from /api/trials/../changes grouped
 *    by day, newest first, human-readable labels, old → new, severity;
 *  - History: version timeline from /versions with any-pair comparison;
 *  - Sources: cross-source mirrors + per-field differences.
 *
 * History is only fetched here — list views never load full history.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { EndpointRow, TrialEvent, TrialVersion, VersionDifference } from "./types";
import { refreshableUrl, useJson } from "./api";
import { compareVersions, isTrialChanges, isTrialMirrors, isTrialVersions } from "./monitoringApi";
import { SOURCE_BADGES, statusTone } from "./labels";
import { fieldLabel, fieldGroupLabel } from "./fieldLabels";
import { ChangeValue, EmptyState, Loading, SeverityBadge, relTime } from "./ui";
import type { Lang, Translate } from "./i18n";
import { navigate, replaceUrl, useRoute } from "./router";
import type { BackendHealth } from "./health";
import { ServiceUnavailable } from "./ui";
import AddToProject from "./AddToProject";
import { RegistryBadge } from "./IntelligenceViz";
import { safeExternalUrl } from "./urlSafety";
import { parseSites } from "./sites";
import { DETAIL_TABS, OVERVIEW_SECTION_IDS, parseDetailTab, type DetailTab } from "./trialViewState";

function fmtEnrollment(n?: number | null): string {
  return typeof n === "number" ? n.toLocaleString() : "—";
}

function Field({ label, children, scrollable = false }: { label: string; children: React.ReactNode; scrollable?: boolean }) {
  return (
    <div className="d-field" role={scrollable ? "region" : undefined}
         aria-label={scrollable ? label : undefined} tabIndex={scrollable ? 0 : undefined}>
      <div className="d-label">{label}</div>
      <div className="d-value">{children}</div>
    </div>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section className="ov-section" id={id}>
      <h2 className="section-title">{title}</h2>
      {children}
    </section>
  );
}

function EndpointTable({ rows, t }: { rows: EndpointRow[]; t: Translate }) {
  return (
    <table className="d-table">
      <thead>
        <tr><th>{t("th.no")}</th><th>{t("th.indicator")}</th><th>{t("th.time")}</th><th>{t("th.type")}</th></tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className="d-table-no">{r.no}</td>
            <td>{r.indicator}</td>
            <td>{r.time}</td>
            <td>{r.type}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Endpoints({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  const isBlock = items.length === 1 && items[0].includes("\n");
  return (
    <Field label={title}>
      {isBlock
        ? <div className="d-pre">{items[0]}</div>
        : <ul className="d-list">{items.map((s, i) => <li key={i}>{s}</li>)}</ul>}
    </Field>
  );
}

/** Trial sites rendered as structured rows (never raw JSON blobs). */
function SitesTable({ raw, t }: { raw: string[]; t: Translate }) {
  const sites = useMemo(() => parseSites(raw), [raw]);

  const [expanded, setExpanded] = useState(false);
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const shownSites = sites.filter((s) =>
    !needle || `${s.facility} ${s.city} ${s.state} ${s.country}`.toLowerCase().includes(needle));
  const limit = expanded ? shownSites.length : 8;

  if (sites.length === 0) return null;
  return (
    <Field label={t("field.sites")} scrollable>
      <div className="sites-head">
        <span className="sites-count">{t("sites.count", { n: sites.length })}</span>
        {sites.length > 4 && (
          <input className="sites-search" value={q} placeholder={t("sites.search")}
                 onChange={(e) => setQ(e.target.value)} aria-label={t("sites.search")} />
        )}
      </div>
      <table className="sites-table">
        <thead>
          <tr>
            <th>{t("sites.facility")}</th><th>{t("sites.city")}</th>
            <th>{t("sites.state")}</th><th>{t("sites.country")}</th>
          </tr>
        </thead>
        <tbody>
          {shownSites.slice(0, limit).map((s, i) => (
            <tr key={i}>
              <td>{s.facility || "—"}</td>
              <td>{s.city || "—"}</td>
              <td>{s.state || "—"}</td>
              <td>{s.country || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shownSites.length > limit && (
        <button type="button" className="more" onClick={() => setExpanded(true)}>
          {t("sites.expand", { n: shownSites.length - limit })}
        </button>
      )}
      {expanded && shownSites.length > 8 && (
        <button type="button" className="text-btn" onClick={() => setExpanded(false)}>{t("sites.collapse")}</button>
      )}
    </Field>
  );
}

interface FullPayload {
  trial?: import("./types").Trial;
  summary?: string | null;
  investigator?: string | null;
  inclusion?: string | null;
  exclusion?: string | null;
  endpointTable?: { primary: EndpointRow[]; secondary: EndpointRow[] } | null;
  changeHistory?: Array<import("./types").ChangeEvent>;
  first_crawled_at?: string | null;
  monitors?: Array<{ id: number; name: string }>;
}

const isFullPayload = (v: unknown): v is FullPayload => typeof v === "object" && v !== null;

export default function TrialDetailPage({ t, lang, health }: { t: Translate; lang: Lang; health: BackendHealth }) {
  const route = useRoute();
  const [source, id] = route.segments.slice(1); // /trials/:source/:id
  const [tab, setTab] = useState<DetailTab>(() => parseDetailTab(route.query.get("tab")));
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    setTab(parseDetailTab(route.query.get("tab")));
  }, [route.url]);

  const selectTab = (next: DetailTab) => {
    setTab(next);
    const params = new URLSearchParams(route.query);
    params.set("tab", next);
    if (next !== "changes") params.delete("event");
    replaceUrl(`/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}?${params}`);
  };

  const [reloadTick, setReloadTick] = useState(0);
  // cached read: returning to a trial (e.g. from a notification, or back from
  // the list) repaints the last payload instantly and revalidates quietly;
  // the retry button bumps the tick for a hard reload
  const fullState = useJson<FullPayload>(
    health.state === "online" && source && id
      ? refreshableUrl(`/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}`, reloadTick)
      : null,
    isFullPayload,
  );
  const full = fullState.status === "ready" ? fullState.data : null;
  const error = fullState.status === "error" ? fullState.message : null;

  const trial = full?.trial;
  const watching0 = false; // resolved after the watches fetch below
  void watching0;

  // watch state: POST/DELETE /watch and optimistically track
  const [watching, setWatching] = useState<boolean | null>(null);
  useEffect(() => {
    if (!trial) return;
    if (watching === null) setWatching(false);
  }, [trial]);
  const fetchWatch = () => {
    fetch(`/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}/watch`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d: unknown) => {
        if (d && typeof d === "object" && (d as { watch?: unknown }).watch) setWatching(true);
        else setWatching(false);
      }).catch(() => undefined);
  };
  useEffect(() => {
    if (health.state === "online" && source && id) fetchWatch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, id, health.state, reloadTick]);
  const toggleWatch = () => {
    const method = watching ? "DELETE" : "POST";
    fetch(`/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}/watch`, { method })
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(() => setWatching(!watching))
      .catch(() => undefined);
  };

  if (health.state !== "online") {
    return <main className="page-main"><ServiceUnavailable health={health} t={t} /></main>;
  }
  if (error) {
    return (
      <main className="page-main">
        <EmptyState title={t("detail.loadFailed")} hint={error}>
          <button type="button" className="more" onClick={() => setReloadTick((n) => n + 1)}>{t("service.retry")}</button>
          <button type="button" className="text-btn" onClick={() => navigate("/trials")}>{t("service.browseTrials")}</button>
        </EmptyState>
      </main>
    );
  }
  if (!full || !trial) {
    return <main className="page-main"><Loading label={t("loading.trialDetail")} /></main>;
  }

  const sponsors = trial.sponsors.filter(Boolean);
  const interventions = trial.interventions.filter(Boolean);
  const changeCount = full.changeHistory?.length;

  return (
    <main className="page-main">
      <nav className="crumbs" aria-label={t("a11y.breadcrumb")}>
        <a href="/trials" onClick={(e) => { e.preventDefault(); navigate("/trials"); }}>{t("nav.trials")}</a>
        <span aria-hidden="true">/</span>
        <span className="tid">{trial.id}</span>
      </nav>

      <header className="trial-head">
        <div className="trial-head-main">
          <div className="trial-head-badges">
            <RegistryBadge registry={trial.source} />
            <span className={`pill ${statusTone(trial.status)}`}>{trial.status}</span>
            <span className="tid trial-head-id">{trial.id}</span>
          </div>
          <h1 className="trial-head-title">{trial.title}</h1>
          {trial.scientificTitle && trial.scientificTitle !== trial.title && (
            <p className="trial-head-sci">{trial.scientificTitle}</p>
          )}
          <div className="trial-head-meta">
            <span>{trial.phase && trial.phase !== "N/A" ? trial.phase : t("card.phaseNa")}</span>
            <span>{trial.studyType ?? "—"}</span>
            <span>{sponsors.length ? sponsors[0] : "—"}</span>
            <span>{t("field.lastUpdated")}: {trial.lastUpdated ?? "—"}</span>
            {typeof changeCount === "number" && (
              <span className="trial-head-changes">{t("head.changesDetected", { n: changeCount })}</span>
            )}
          </div>
        </div>
        <div className="trial-head-actions">
          <AddToProject kind="trials" source={source} trialId={id} />
          <button type="button" className={`chip ${watching ? "chip-on" : ""}`}
                  aria-pressed={!!watching} onClick={toggleWatch}>
            {watching ? t("trial.watching") : t("trial.watch")}
          </button>
          <button type="button" className="chip" onClick={() => {
            try { sessionStorage.setItem("ct-trial-compare-add", JSON.stringify(trial)); } catch { /* optional session state */ }
            navigate("/trials");
          }}>{t("preview.addCompare")}</button>
          {safeExternalUrl(trial.url) && <a className="reg-link" href={safeExternalUrl(trial.url)!} target="_blank" rel="noreferrer">{t("link.registry")}</a>}
        </div>
      </header>

      <div className="trial-print-meta" aria-hidden="true">{trial.id} · {SOURCE_BADGES[trial.source]?.label ?? trial.source} · {new Date().toLocaleString()}</div>

      <div className="page-tabs trial-detail-tabs" role="tablist" aria-label={t("a11y.detailTabs")}>
        {DETAIL_TABS.map((x, index) => (
          <button ref={(node) => { tabRefs.current[index] = node; }} id={`trial-tab-${x}`} type="button" key={x} role="tab" aria-selected={tab === x}
                  aria-controls={`trial-panel-${x}`} tabIndex={tab === x ? 0 : -1}
                  className={`ptab ${tab === x ? "ptab-on" : ""}`}
                  onClick={() => selectTab(x)} onKeyDown={(event) => {
                    let next = index;
                    if (event.key === "ArrowRight") next = (index + 1) % DETAIL_TABS.length;
                    else if (event.key === "ArrowLeft") next = (index - 1 + DETAIL_TABS.length) % DETAIL_TABS.length;
                    else if (event.key === "Home") next = 0;
                    else if (event.key === "End") next = DETAIL_TABS.length - 1;
                    else return;
                    event.preventDefault(); selectTab(DETAIL_TABS[next]); tabRefs.current[next]?.focus();
                  }}>
            {t(`detail.tab.${x}` as never)}
            {x === "changes" && typeof changeCount === "number" ? ` · ${changeCount}` : ""}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div role="tabpanel" id="trial-panel-overview" aria-labelledby="trial-tab-overview"><OverviewTab full={full} trial={trial} sponsors={sponsors} interventions={interventions}
                     t={t} lang={lang} health={health} watching={!!watching} />
        </div>
      )}
      {tab === "changes" && <div role="tabpanel" id="trial-panel-changes" aria-labelledby="trial-tab-changes"><ChangesTab key={`${source}/${id}`} source={source} id={id} t={t} lang={lang} /></div>}
      {tab === "history" && <div role="tabpanel" id="trial-panel-history" aria-labelledby="trial-tab-history"><HistoryTab key={`${source}/${id}`} source={source} id={id} t={t} lang={lang} /></div>}
      {tab === "sources" && <div role="tabpanel" id="trial-panel-sources" aria-labelledby="trial-tab-sources"><SourcesTab key={`${source}/${id}`} source={source} id={id} t={t} lang={lang} trialStatus={trial.status} trialEnrollment={trial.enrollment} trialLastUpdated={trial.lastUpdated} /></div>}
    </main>
  );
}

/* ── Overview ─────────────────────────────────────────────────────────── */

function OverviewSectionNav({ t }: { t: Translate }) {
  const items: Array<[typeof OVERVIEW_SECTION_IDS[number], Parameters<Translate>[0]]> = [
    ["study", "ov.study"], ["conditions-interventions", "ov.condInterv"],
    ["outcomes", "ov.outcomes"], ["eligibility", "ov.eligibility"],
    ["locations", "ov.locations"], ["sponsor-collaborators", "ov.sponsor"],
    ["provenance", "ov.provenance"],
  ];
  const [active, setActive] = useState(items[0][0]);
  useEffect(() => {
    if (!("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (visible) setActive(visible.target.id as typeof active);
    }, { rootMargin: "-120px 0px -65%", threshold: [0, .1] });
    items.forEach(([id]) => { const node = document.getElementById(id); if (node) observer.observe(node); });
    return () => observer.disconnect();
  }, []);
  return <nav className="overview-section-nav" aria-label={t("ov.sectionsAria")}>
    {items.map(([id, key]) => <a key={id} href={`#${id}`} aria-current={active === id ? "location" : undefined}
      onClick={(event) => {
        event.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
        history.replaceState(history.state, "", `${location.pathname}${location.search}#${id}`);
      }}>{t(key)}</a>)}
  </nav>;
}

function EligibilityCriteria({ inclusion, exclusion, t }: { inclusion?: string | null; exclusion?: string | null; t: Translate }) {
  const [expanded, setExpanded] = useState({ inclusion: false, exclusion: false });
  const criteria = [
    { key: "inclusion" as const, label: t("criteria.inclusion"), text: inclusion },
    { key: "exclusion" as const, label: t("criteria.exclusion"), text: exclusion },
  ].filter((item) => item.text);
  const allExpanded = criteria.every((item) => expanded[item.key]);
  return <div className="d-criteria">
    <div className="criteria-tools">
      <button type="button" className="text-btn" onClick={() => setExpanded({ inclusion: true, exclusion: true })}>{t("criteria.expandAll")}</button>
      <button type="button" className="text-btn" onClick={() => setExpanded({ inclusion: false, exclusion: false })}>{t("criteria.collapseAll")}</button>
      <span className="sr-only" aria-live="polite">{allExpanded ? t("criteria.allExpanded") : ""}</span>
    </div>
    {criteria.map((item) => {
      const open = expanded[item.key];
      const contentId = `criteria-${item.key}`;
      return <div className={`criteria-box criteria-${item.key === "inclusion" ? "in" : "ex"} ${open ? "is-expanded" : "is-collapsed"}`} key={item.key}>
        <div className="criteria-head"><span>{item.label}</span><button type="button" className="text-btn" aria-expanded={open} aria-controls={contentId}
          onClick={() => setExpanded((current) => ({ ...current, [item.key]: !current[item.key] }))}>{open ? t("criteria.collapse") : t("criteria.expand")}</button></div>
        <div className="criteria-body" id={contentId}>{item.text}</div>
      </div>;
    })}
  </div>;
}

function OverviewTab({ full, trial, sponsors, interventions, t, lang, health, watching }: {
  full: FullPayload; trial: import("./types").Trial;
  sponsors: string[]; interventions: string[];
  t: Translate; lang: Lang; health: BackendHealth; watching: boolean;
}) {
  void lang;
  const table = full.endpointTable;
  const lastChange = full.changeHistory?.[full.changeHistory.length - 1];
  return (
    <>
      <OverviewSectionNav t={t} />
      <div className="detail-layout">
      <div className="detail-main">
        <Section id="study" title={t("ov.study")}>
          {full.summary && <Field label={t("field.purpose")}><div className="d-value d-summary">{full.summary}</div></Field>}
          <div className="d-grid">
            <Field label={t("field.studyType")}>{trial.studyType ?? "—"}</Field>
            <Field label={t("field.phase")}>{trial.phase && trial.phase !== "N/A" ? trial.phase : "—"}</Field>
            <Field label={t("field.enrollment")}>{fmtEnrollment(trial.enrollment)}</Field>
            <Field label={t("field.startDate")}>{trial.startDate ?? "—"}</Field>
            <Field label={t("field.completionDate")}>{trial.completionDate ?? "—"}</Field>
            <Field label={t("field.registration")}>{trial.registrationDate ?? "—"}</Field>
          </div>
        </Section>

        {(trial.conditions.length > 0 || interventions.length > 0) && (
          <Section id="conditions-interventions" title={t("ov.condInterv")}>
            {trial.conditions.length > 0 && (
              <Field label={t("field.conditions")}>
                <div className="tags">{trial.conditions.map((c, i) => <span className="tag" key={i}>{c}</span>)}</div>
              </Field>
            )}
            {interventions.length > 0 && <Field label={t("field.intervention")}>{interventions.join(" · ")}</Field>}
          </Section>
        )}

        <Section id="outcomes" title={t("ov.outcomes")}>
          <Field label={t("field.primaryEndpoint")} scrollable={(table?.primary?.length ?? 0) > 0}>
            {table?.primary?.length
              ? <EndpointTable rows={table.primary} t={t} />
              : <div className="d-pre">{trial.primaryEndpoint ?? "—"}</div>}
          </Field>
          {(table?.secondary?.length || trial.secondaryEndpoints.length > 0) && (
            <Field label={t("field.secondaryEndpoints")} scrollable={(table?.secondary?.length ?? 0) > 0}>
              {table?.secondary?.length
                ? <EndpointTable rows={table.secondary} t={t} />
                : <Endpoints title="" items={trial.secondaryEndpoints} />}
            </Field>
          )}
        </Section>

        {(full.inclusion || full.exclusion) && (
          <Section id="eligibility" title={t("ov.eligibility")}>
            <EligibilityCriteria inclusion={full.inclusion} exclusion={full.exclusion} t={t} />
          </Section>
        )}

        {trial.locations.length > 0 && (
          <Section id="locations" title={t("ov.locations")}>
            <SitesTable raw={trial.locations} t={t} />
          </Section>
        )}

        <Section id="sponsor-collaborators" title={t("ov.sponsor")}>
          <Field label={t("field.sponsor")}>{sponsors.length ? sponsors.join(" · ") : "—"}</Field>
          {trial.countries.length > 0 && <Field label={t("field.countries")}>{trial.countries.join(", ")}</Field>}
          {full.investigator && <Field label={t("field.investigator")}>{full.investigator}</Field>}
        </Section>

        <Section id="provenance" title={t("ov.provenance")}>
          <dl className="provenance-list">
            <div><dt>{t("ov.displaySource")}</dt><dd>{SOURCE_BADGES[trial.source]?.label ?? trial.source}</dd></div>
            <div><dt>{t("sources.id")}</dt><dd className="tid">{trial.id}</dd></div>
            <div><dt>{t("field.lastUpdated")}</dt><dd>{trial.lastUpdated ?? t("common.notProvided")}</dd></div>
            <div><dt>{t("search.compare.inLibrary")}</dt><dd>{full.first_crawled_at ?? t("common.notProvided")}</dd></div>
            <div><dt>{t("ov.monitorships")}</dt><dd>{full.monitors?.length ? full.monitors.map((monitor) => monitor.name).join(" · ") : t("common.notProvided")}</dd></div>
          </dl>
          <div className="provenance-actions">
            {safeExternalUrl(trial.url) && <a className="text-btn" href={safeExternalUrl(trial.url)!} target="_blank" rel="noreferrer">{t("link.registry")} ↗</a>}
            <button type="button" className="text-btn" onClick={() => navigate(`/trials/${encodeURIComponent(trial.source)}/${encodeURIComponent(trial.id)}?tab=sources`)}>{t("ov.openSources")}</button>
            <button type="button" className="text-btn" onClick={() => navigate("/data-sources")}>{t("search.dataSourcesLink")}</button>
          </div>
        </Section>
      </div>

      <aside className="detail-rail" aria-label={t("ov.railAria")}>
        <div className="rail-box">
          <h3 className="rail-title">{t("ov.railStatus")}</h3>
          <span className={`pill ${statusTone(trial.status)}`}>{trial.status}</span>
          <dl className="rail-list">
            <div><dt>{t("field.lastUpdated")}</dt><dd>{trial.lastUpdated ?? "—"}</dd></div>
            <div><dt>{t("ov.lastChange")}</dt><dd>{lastChange ? relTime(lastChange.detected_at) : t("timeline.noChanges")}</dd></div>
            <div><dt>{t("ov.changeCount")}</dt><dd>{full.changeHistory?.length ?? 0}</dd></div>
            <div><dt>{t("ov.watchState")}</dt><dd>{watching ? t("trial.watching") : t("trial.unwatchLabel")}</dd></div>
            <div><dt>{t("ov.registry")}</dt><dd>{SOURCE_BADGES[trial.source]?.label ?? trial.source}</dd></div>
          </dl>
          {(full.monitors?.length ?? 0) > 0 && (
            <div className="rail-monitors">
              <span className="rail-monitors-label">{t("ov.monitorships")}</span>
              {full.monitors!.map((m) => (
                <button type="button" key={m.id} className="text-btn rail-monitor-link"
                        onClick={() => navigate(`/monitors/${m.id}`)}>{m.name}</button>
              ))}
            </div>
          )}
          <div className="rail-source-actions">
            {safeExternalUrl(trial.url) && <a className="text-btn" href={safeExternalUrl(trial.url)!} target="_blank" rel="noreferrer">{t("link.registry")} ↗</a>}
            <button type="button" className="text-btn" onClick={() => navigate("/data-sources")}>{t("search.dataSourcesLink")}</button>
          </div>
        </div>
      </aside>
      </div>
    </>
  );
  void health;
}

/* ── Changes ──────────────────────────────────────────────────────────── */

function ChangesTab({ source, id, t, lang }: { source: string; id: string; t: Translate; lang: Lang }) {
  const route = useRoute();
  const targetEvent = Number(route.query.get("event") ?? "") || null;
  const [sev, setSev] = useState("");
  // cached read keyed on trial+severity: re-opening the tab (or the same
  // notification link) paints instantly; a severity switch loads fresh
  const changesState = useJson<{ total: number; events: TrialEvent[] }>(
    `/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}/changes?limit=500${sev ? `&severity=${encodeURIComponent(sev)}` : ""}`,
    isTrialChanges,
  );
  const events = changesState.status === "ready" ? changesState.data.events : null;
  const error = changesState.status === "error" ? changesState.message : null;

  useEffect(() => {
    if (!targetEvent || !events) return;
    document.getElementById(`event-${targetEvent}`)?.scrollIntoView({ block: "center" });
  }, [targetEvent, events]);

  if (error) return <EmptyState title={t("changes.loadFailed")} hint={error} />;
  if (!events) return <Loading label={t("loading.changes")} />;

  const byDay = new Map<string, typeof events>();
  for (const e of [...events].sort((a, b) => b.detected_at.localeCompare(a.detected_at))) {
    const day = (e.detected_at || "").slice(0, 10) || "—";
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day)!.push(e);
  }

  return (
    <section aria-label={t("detail.tab.changes")}>
      <div className="range-chips changes-filter" role="group" aria-label={t("changes.sevFilter")}>
        {["", "critical", "important", "normal", "minor"].map((s) => (
          <button type="button" key={s || "all"} className={`chip ${sev === s ? "chip-on" : ""}`}
                  aria-pressed={sev === s} onClick={() => setSev(s)}>
            {s ? t(`sev.${s}` as never) : t("chip.all")}
          </button>
        ))}
      </div>
      {events.length === 0 && <EmptyState title={t("changes.empty")} hint={t("changes.emptyHint")} />}
      <div className="tl">
        {[...byDay.entries()].map(([day, evs]) => (
          <div className="tl-day" key={day}>
            <div className="tl-rail">
              <span className="tl-dot" />
              <span className="tl-line" />
            </div>
            <div className="tl-body">
              <div className="tl-date">{day}</div>
              {evs.map((ev) => (
                <article className={`change-row ${targetEvent === ev.event_id ? "change-row-target" : ""}`} id={`event-${ev.event_id}`} key={ev.event_id}>
                  <SeverityBadge severity={ev.severity} t={t} />
                  <AddToProject kind="evidence" eventId={ev.event_id} />
                  <div className="change-main">
                    <b>{fieldLabel(ev.field_name, lang, ev)}</b>
                    {ev.field_group && <small className="change-group">{fieldGroupLabel(ev.field_group, lang)}</small>}
                    <div className="update-diff">
                      <ChangeValue value={ev.old_value} />
                      <span className="diff-arrow">→</span>
                      <ChangeValue value={ev.new_value} />
                    </div>
                    <small className="change-meta">
                      <RegistryBadge registry={ev.source} />
                      <span title={ev.detected_at}>{relTime(ev.detected_at)}</span>
                      <span className="event-id">#{ev.event_id}</span>
                    </small>
                  </div>
                </article>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── History (versions + comparison) ──────────────────────────────────── */

function HistoryTab({ source, id, t, lang }: { source: string; id: string; t: Translate; lang: Lang }) {
  const [fromV, setFromV] = useState<number | null>(null);
  const [toV, setToV] = useState<number | null>(null);
  const [diff, setDiff] = useState<VersionDifference[] | null>(null);
  const [diffBusy, setDiffBusy] = useState(false);
  const [allFields, setAllFields] = useState(false);
  // cached read keyed on trial (remounted per trial below)
  const versionsState = useJson<{ versions: TrialVersion[] }>(
    `/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}/versions`, isTrialVersions);
  const versions = versionsState.status === "ready" ? versionsState.data.versions : null;
  const error = versionsState.status === "error" ? versionsState.message : null;

  useEffect(() => {
    if (!versions) return;
    const nums = versions.map((v) => v.version_number).sort((a, b) => a - b);
    if (nums.length >= 2) {
      setFromV(nums[nums.length - 2]);
      setToV(nums[nums.length - 1]);
    } else if (nums.length === 1) {
      setToV(nums[0]);
      setFromV(nums[0]);
    }
  }, [versions]);

  const runCompare = () => {
    if (fromV === null || toV === null || fromV === toV) return;
    setDiffBusy(true);
    compareVersions(source, id, Math.min(fromV, toV), Math.max(fromV, toV))
      .then((x) => { setDiff(x.changes); setDiffBusy(false); })
      .catch(() => setDiffBusy(false));
  };

  if (error) return <EmptyState title={t("history.loadFailed")} hint={error} />;
  if (!versions) return <Loading label={t("loading.history")} />;
  if (versions.length === 0) return <EmptyState title={t("history.empty")} />;
  if (versions.length === 1) {
    return (
      <EmptyState title={t("history.single", { v: versions[0].version_number })}
                  hint={versions[0].retrieved_at?.slice(0, 10) ?? ""} />
    );
  }

  return (
    <section aria-label={t("detail.tab.history")}>
      <div className="compare-box">
        <label>
          {t("history.older")}
          <select value={fromV ?? ""} onChange={(e) => setFromV(Number(e.target.value))}>
            {versions.map((v) => <option key={v.version_number} value={v.version_number}>v{v.version_number}</option>)}
          </select>
        </label>
        <span className="diff-arrow">→</span>
        <label>
          {t("history.newer")}
          <select value={toV ?? ""} onChange={(e) => setToV(Number(e.target.value))}>
            {versions.map((v) => <option key={v.version_number} value={v.version_number}>v{v.version_number}</option>)}
          </select>
        </label>
        <button type="button" className="chip chip-on" disabled={diffBusy || fromV === toV} onClick={runCompare}>
          {t("history.compare")}
        </button>
        <label className="compare-toggle">
          <input type="checkbox" checked={allFields} onChange={(e) => setAllFields(e.target.checked)} />
          {t("history.allFields")}
        </label>
      </div>

      {diff !== null && (
        <div className="comparison">
          <h3 className="section-title">{t("history.diffTitle", { from: Math.min(fromV!, toV!), to: Math.max(fromV!, toV!) })}</h3>
          {diff.length === 0 && <EmptyState title={t("history.noDiff")} />}
          {diff.filter((c) => allFields || c.old_value !== c.new_value).map((c, i) => (
            <div className="comparison-row" key={i}>
              <b>{fieldLabel(c.field_name, lang, c)}</b>
              <div className="update-diff">
                <ChangeValue value={c.old_value} />
                <span className="diff-arrow">→</span>
                <ChangeValue value={c.new_value} />
              </div>
              <small>
                {c.change_type}
                {c.absolute_change !== undefined && ` · ${c.absolute_change > 0 ? "+" : ""}${c.absolute_change}`}
                {c.percent_change !== undefined && ` (${c.percent_change}%)`}
                {c.date_shift_days !== undefined && ` · ${t("history.shiftDays", { n: c.date_shift_days })}`}
              </small>
            </div>
          ))}
        </div>
      )}

      <h3 className="section-title">{t("history.timelineTitle")}</h3>
      <div className="tl">
        {versions.map((v, i) => (
          <div className="tl-day" key={v.id}>
            <div className="tl-rail">
              <span className="tl-dot" />
              <span className="tl-line" />
            </div>
            <div className="tl-body">
              <div className="tl-date">
                {t("history.version", { n: v.version_number })} · {v.retrieved_at?.slice(0, 10) ?? "—"}
              </div>
              <div className="history-row-meta">
                <RegistryBadge registry={v.registry} />
                <span>{t("history.changesInVersion", { n: v.change_count })}</span>
                {v.highest_change_severity && <SeverityBadge severity={v.highest_change_severity} t={t} />}
                {v.source_updated_at && <small>{t("history.sourceUpdated")}: {v.source_updated_at.slice(0, 10)}</small>}
                {i < versions.length - 1 && versions[i + 1] && (
                  <button type="button" className="text-btn"
                          onClick={() => {
                            setFromV(versions[i + 1].version_number);
                            setToV(v.version_number);
                            compareVersions(source, id, versions[i + 1].version_number, v.version_number)
                              .then((x) => setDiff(x.changes))
                              .catch(() => undefined);
                          }}>
                    {t("history.compareToPrev")}
                  </button>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Sources (cross-source mirrors) ───────────────────────────────────── */

function SourcesTab({ source, id, t, lang, trialStatus, trialEnrollment, trialLastUpdated }: {
  source: string; id: string; t: Translate; lang: Lang;
  trialStatus: string; trialEnrollment?: number | null; trialLastUpdated?: string | null;
}) {
  // cached read keyed on trial (remounted per trial below)
  const mirrorsState = useJson(`/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}/mirrors`, isTrialMirrors);
  const state = mirrorsState.status === "ready"
    ? { total: mirrorsState.data.total, siblings: mirrorsState.data.siblings as Array<Record<string, unknown>> }
    : null;
  const error = mirrorsState.status === "error" ? mirrorsState.message : null;

  if (error) return <EmptyState title={t("sources.loadFailed")} hint={error} />;
  if (!state) return <Loading label={t("loading.sources")} />;

  return (
    <section aria-label={t("detail.tab.sources")}>
      {state.total === 0 ? (
        <EmptyState title={t("sources.empty")} hint={t("sources.emptyHint")} />
      ) : (
        <>
          <h2 className="section-title">{t("sources.recordsTitle")}</h2>
          <table className="d-table sources-table">
            <thead>
              <tr>
                <th>{t("sources.registry")}</th>
                <th>{t("sources.id")}</th>
                <th>{t("status.all")}</th>
                <th>{t("field.enrollment")}</th>
                <th>{t("field.lastUpdated")}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{SOURCE_BADGES[source]?.label ?? source}</td>
                <td className="tid">{id}</td>
                <td>{trialStatus}</td>
                <td>{fmtEnrollment(trialEnrollment)}</td>
                <td>{trialLastUpdated?.slice(0, 10) ?? "—"}</td>
                <td>{t("sources.thisRecord")}</td>
              </tr>
              {state.siblings.map((s, i) => {
                const sibSource = String(s.short_name ?? "");
                const sibId = String(s.source_trial_id ?? "");
                return (
                  <tr key={i}>
                    <td>
                      <RegistryBadge registry={sibSource} />
                    </td>
                    <td className="tid">
                      {sibId ? <TrialLinkInline source={sibSource} id={sibId} /> : "—"}
                    </td>
                    <td>{String(s.status ?? "—")}</td>
                    <td>{typeof s.enrollment === "number" ? s.enrollment.toLocaleString() : "—"}</td>
                    <td>{String(s.last_updated_at_source ?? "—").slice(0, 10)}</td>
                    <td>
                      {s.source_url
                        && safeExternalUrl(s.source_url) ? <a className="reg-link" href={safeExternalUrl(s.source_url)!} target="_blank" rel="noreferrer">{t("link.registry")}</a>
                        : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <h2 className="section-title">{t("sources.diffsTitle")}</h2>
          <table className="d-table sources-table">
            <thead>
              <tr>
                <th>{t("sources.field")}</th>
                <th>{SOURCE_BADGES[source]?.label ?? source}</th>
                <th>{lang === "zh" ? "其他源" : "Other source"}</th>
              </tr>
            </thead>
            <tbody>
              {state.siblings.map((s, i) => {
                const sibSource = String(s.short_name ?? "");
                const rows: Array<[string, unknown, unknown]> = [
                  [t("status.all"), trialStatus, s.status ?? "—"],
                  [t("field.enrollment"), trialEnrollment ?? "—", s.enrollment ?? "—"],
                ];
                return rows.filter(([, a, b]) => String(a ?? "") !== String(b ?? "")).map(([label, a, b], j) => (
                  <tr key={`${i}-${j}`}>
                    <td>{label}</td>
                    <td>{String(a ?? "—")}</td>
                    <td>{String(b ?? "—")} <small className="change-meta">({SOURCE_BADGES[sibSource]?.label ?? sibSource} · {String(s.source_trial_id ?? "")})</small></td>
                  </tr>
                ));
              }).flat()}
            </tbody>
          </table>
          {state.siblings.every((s) =>
            String(s.status ?? "") === trialStatus && String(s.enrollment ?? "") === String(trialEnrollment ?? "")
          ) && <EmptyState title={t("sources.noDiffs")} />}
        </>
      )}
    </section>
  );
}

function TrialLinkInline({ source, id }: { source: string; id: string }) {
  return (
    <a href={`/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}`}
       onClick={(e) => {
         if (e.metaKey || e.ctrlKey || e.button !== 0) return;
         e.preventDefault();
         navigate(`/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}`);
       }}>
      {id}
    </a>
  );
}
