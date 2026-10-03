import { useEffect, useMemo, useState } from "react";
import { useJson } from "./api";
import { navigate, useRoute } from "./router";
import { EmptyState, Loading, SeverityBadge, ServiceUnavailable } from "./ui";
import type { BackendHealth } from "./health";
import type { Translate } from "./i18n";
import type { Lang } from "./i18n";
import { fieldLabel } from "./fieldLabels";
import { isProjectList } from "./projectApi";
import ScopeHeader from "./ScopeHeader";
import { ActivityTrend, categoryLabel, FreshnessBadge, KpiStrip, PageSection, RegistryBadge } from "./IntelligenceViz";
import { isScopedUpdates } from "./monitoringApi";
import type { ScopedUpdatesData } from "./types";

type EventItem = { event_id: number; trial_id: string; source: string; title: string; field_name: string; old_value?: string | null; new_value?: string | null; old_label?: string; new_label?: string; severity: string; detected_at: string; delta?: number; percent?: number | null; delta_days?: number; direction?: string; added?: string[]; removed?: string[]; };
type Member = { id: number; monitor_id: number; monitor_name: string; trial_id: string; event_type: string; detected_at: string; };
type Section = { key: string; title: string; count: number; items?: EventItem[] | Member[]; new_trials?: Array<{ trial_id: string; title: string; source: string }>; membership?: Member[]; groups?: Array<{ from: string; to: string; count: number; items: EventItem[] }>; enrollment?: EventItem[]; increases?: EventItem[]; decreases?: EventItem[]; timeline?: EventItem[]; sponsors?: Array<{ name: string; new_trials: number; changed_trials: number; priority_changes: number }>; countries?: EventItem[]; sources?: Array<{ name: string; state: string; last_success?: string | null; stale_reason?: string; data_as_of?: string | null }>; disagreements?: { logical_trials: number; difference_fields: number; items: Array<{ master_trial_id: string; fields: string[]; sources: Array<{ source: string; trial_id: string; title: string }> }> }; };
type Bucket = { date: string; critical: number; important: number; normal: number; minor: number; entered: number; left: number; reentered: number; };
type Briefing = { counts: Record<string, number>; summary: string; sections: Section[]; freshness: { affected: Array<{ name: string; state: string; reason?: string }>; unknown: Array<{ name: string }>; disabled: Array<{ name: string }>; complete: boolean }; trends: Bucket[]; categories: Record<string, number>; source_activity: Array<{ source: string; new_trials: number; changed_trials: number; priority_changes: number }>; most_frequently_updated: Array<{ trial_id: string; title: string; source: string; count: number; highest_severity: string; items: EventItem[] }>; };
type Comparison = { monitors: Array<{ monitor_id: number; name: string; current_trials: number; entered: number; left: number; reentered: number; changed_trials: number; critical: number; important: number; last_run?: string | null }> };
const isBriefing = (v: unknown): v is Briefing => typeof v === "object" && v !== null && typeof (v as Briefing).summary === "string" && Array.isArray((v as Briefing).sections);
const isComparison = (v: unknown): v is Comparison => typeof v === "object" && v !== null && Array.isArray((v as Comparison).monitors);

/** Backend stale verdict detail (registry_health.stale_reason) → localized
 *  label; empty for OK.  The verdict itself always comes from the backend —
 *  the UI never derives staleness from timestamps on its own. */
function reasonLabel(reason: string | undefined, t: Translate): string {
  if (!reason || reason === "OK") return "";
  const key = `intel.reason.${reason}` as Parameters<Translate>[0];
  const label = t(key);
  return label === key ? reason.toLowerCase().replace(/_/g, " ") : label;
}

function EventRow({ item, t, lang }: { item: EventItem; t: Translate; lang: Lang }) {
  return <button type="button" className="briefing-event" onClick={() => navigate(`/trials/${encodeURIComponent(item.source)}/${encodeURIComponent(item.trial_id)}?tab=changes&event=${item.event_id}`)}>
    <SeverityBadge severity={item.severity} t={t} /><span><b>{item.title}</b><small><RegistryBadge registry={item.source} /> · {item.trial_id} · {fieldLabel(item.field_name, lang)}</small></span>
    <span className="briefing-diff">{(item.old_label ?? item.old_value ?? "—").slice(0, 80)} → {(item.new_label ?? item.new_value ?? "—").slice(0, 80)}{item.delta !== undefined && ` (${item.delta > 0 ? "+" : ""}${item.delta}${item.percent !== null && item.percent !== undefined ? `, ${item.percent}%` : ""})`}{item.delta_days !== undefined && ` (${item.direction}, ${Math.abs(item.delta_days)}d)`}{item.added && (item.added.length || item.removed?.length) ? ` (+${item.added.join(", ")} −${item.removed?.join(", ") ?? ""})` : ""}</span>
  </button>;
}

/** Preview depth for one expanded "What changed" category. */
const CATEGORY_PREVIEW = 8;

/** One "What changed" category row; clicking expands the latest events in it. */
function CategoryRow({ category, count, maxCount, open, onToggle, t }: {
  category: string; count: number; maxCount: number;
  open: boolean; onToggle: () => void; t: Translate;
}) {
  const detailId = "briefing-category-detail";
  return <div className={`briefing-cat ${open ? "briefing-cat-open" : ""}`}>
    <button type="button" className="briefing-cat-row" aria-expanded={open} aria-controls={detailId}
            title={t("intel.category.details")} onClick={onToggle}>
      <span className="briefing-cat-label">{categoryLabel(category, t)}</span>
      <span className="briefing-cat-track" aria-hidden><i style={{ width: `${count / Math.max(1, maxCount) * 100}%` }} /></span>
      <strong className="briefing-cat-count">{count.toLocaleString()}</strong>
      <span className="briefing-cat-chevron" aria-hidden>{open ? "▾" : "▸"}</span>
    </button>
  </div>;
}

export default function BriefingPage({ health, t, lang }: { health: BackendHealth; t: Translate; lang: Lang }) {
  const route = useRoute();
  const [openCategory, setOpenCategory] = useState<string | null>(null);
  const scope = ["monitored", "monitor", "watched", "all", "project"].includes(route.query.get("scope") ?? "") ? route.query.get("scope")! : "monitored";
  const window = ["24h", "7d", "30d"].includes(route.query.get("window") ?? "") ? route.query.get("window")! : "7d";
  const registry = route.query.get("registry") ?? "all";
  const monitor = route.query.get("monitor") ?? "";
  const project = route.query.get("project_id") ?? "";
  const projects = useJson("/api/projects", isProjectList);
  const view = route.query.get("view") === "monitors" ? "monitors" : "briefing";
  const params = useMemo(() => new URLSearchParams({ scope, window, registry, ...(scope === "monitor" && monitor ? { monitor_id: monitor } : {}), ...(scope === "project" && project ? { project_id: project } : {}) }), [scope, window, registry, monitor, project]);
  // shared filter context for the category drill-down: fetchQuery hits
  // /api/updates directly, linkQuery opens the Updates page with the same scope
  const fetchQuery = useMemo(() => ({ scope, window, ...(registry !== "all" ? { registry } : {}), ...(scope === "monitor" && monitor ? { monitor_id: monitor } : {}), ...(scope === "project" && project ? { project_id: project } : {}) }), [scope, window, registry, monitor, project]);
  const linkQuery = { scope, window, ...(registry !== "all" ? { registry } : {}), ...(scope === "monitor" && monitor ? { monitor } : {}), ...(scope === "project" && project ? { project_id: project } : {}) };
  const categoryDetails = useJson<ScopedUpdatesData>(health.state === "online" && view === "briefing" && openCategory
    ? `/api/updates?${new URLSearchParams({ ...fetchQuery, category: openCategory, page_size: String(CATEGORY_PREVIEW) })}`
    : null, isScopedUpdates);
  const briefing = useJson<Briefing>(health.state === "online" && view === "briefing" && (scope !== "monitor" || monitor) ? `/api/intelligence/briefing?${params}` : null, isBriefing);
  const comparison = useJson<Comparison>(health.state === "online" && view === "monitors" ? `/api/intelligence/monitors?window=${window}&registry=${registry}` : null, isComparison);
  const monitorList = useJson<{ monitors: Array<{ id: number; name: string }> }>(health.state === "online" ? "/api/monitors" : null,
    (v): v is { monitors: Array<{ id: number; name: string }> } => typeof v === "object" && v !== null && Array.isArray((v as { monitors?: unknown }).monitors));
  const update = (part: Record<string, string>) => { const q = new URLSearchParams(route.query); Object.entries(part).forEach(([k, v]) => v ? q.set(k, v) : q.delete(k)); navigate(`/briefing?${q}`); };
  useEffect(() => { if (scope === "monitor" && !monitor && monitorList.status === "ready" && monitorList.data.monitors.length) update({ monitor: String(monitorList.data.monitors[0].id) }); }, [scope, monitor, monitorList.status]);
  if (health.state !== "online") return <main className="page-main"><ServiceUnavailable health={health} t={t} /></main>;
  const data = briefing.status === "ready" ? briefing.data : null;
  const categoryRows = data ? Object.entries(data.categories).sort((a, b) => b[1] - a[1]) : [];
  const attentionItems = data ? data.sections.flatMap((section) => section.key === "priority" ? (section.items as EventItem[] | undefined) ?? [] : []).slice(0, 8) : [];
  const maxCategoryCount = Math.max(1, ...categoryRows.map(([, count]) => count));
  const scopeTitle = scope === "project" ? projects.status === "ready" ? projects.data.projects.find((p) => String(p.id) === project)?.name ?? "Project" : "Project"
    : scope === "monitor" ? monitorList.status === "ready" ? monitorList.data.monitors.find((m) => String(m.id) === monitor)?.name ?? "Monitor" : "Monitor"
    : scope === "watched" ? t("intel.watched") : scope === "all" ? t("intel.allIndexed") : t("intel.allMonitored");
  return <main className="page-main intelligence-page briefing-page"><div className="page-head briefing-page-head"><div><span className="scope-eyebrow">{t("intel.briefing")}</span><h1>{t("intel.title")}</h1><p className="page-sub">{t("intel.sub")}</p></div><div className="briefing-head-actions"><button className="chip" type="button" onClick={() => navigate(`/updates?${new URLSearchParams(linkQuery)}`)}>{t("intel.viewUpdates")}</button><button className="chip" type="button" onClick={() => globalThis.print()}>{t("intel.print")}</button></div></div>
    <div className="page-tabs" role="tablist" aria-label={t("intel.title")}><button type="button" role="tab" aria-selected={view === "briefing"} className={`ptab ${view === "briefing" ? "ptab-on" : ""}`} onClick={() => update({ view: "" })}>{t("intel.briefing")}</button><button type="button" role="tab" aria-selected={view === "monitors"} className={`ptab ${view === "monitors" ? "ptab-on" : ""}`} onClick={() => update({ view: "monitors" })}>{t("intel.monitorComparison")}</button></div>
    <ScopeHeader t={t} level={2} eyebrow={t("intel.briefing")} title={scopeTitle} description={t("intel.sub")} meta={`${window} · ${registry === "all" ? t("intel.allRegistries") : registry}`} />
    <div className="toolbar toolbar-static briefing-filters" role="group" aria-label={t("intel.filters")}>
      <select aria-label={t("intel.scope")} value={scope} onChange={(e) => update({ scope: e.target.value, monitor: e.target.value === "monitor" ? monitor || String(monitorList.status === "ready" ? monitorList.data.monitors[0]?.id ?? "" : "") : "", project_id: e.target.value === "project" ? project || String(projects.status === "ready" ? projects.data.projects[0]?.id ?? "" : "") : "" })}><option value="monitored">{t("intel.allMonitored")}</option><option value="monitor">{t("intel.oneMonitor")}</option><option value="watched">{t("intel.watched")}</option><option value="all">{t("intel.allIndexed")}</option><option value="project">{t("ws.project")}</option></select>
      {scope === "monitor" && <select aria-label={t("intel.oneMonitor")} value={monitor} onChange={(e) => update({ monitor: e.target.value })}>{monitorList.status === "ready" && monitorList.data.monitors.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select>}
      {scope === "project" && <select aria-label={t("ws.project")} value={project} onChange={(e) => update({ project_id: e.target.value })}>{projects.status === "ready" && projects.data.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>}
      <select aria-label={t("intel.window")} value={window} onChange={(e) => update({ window: e.target.value })}><option value="24h">{t("intel.daily")}</option><option value="7d">{t("intel.weekly")}</option><option value="30d">{t("intel.monthly")}</option></select>
      <select aria-label={t("intel.registry")} value={registry} onChange={(e) => update({ registry: e.target.value })}><option value="all">{t("intel.allRegistries")}</option><option value="clinicaltrials_gov">ClinicalTrials.gov</option><option value="chictr">ChiCTR</option><option value="chinadrugtrials">CTR</option><option value="who_ictrp">WHO ICTRP</option></select>
    </div>
    {view === "monitors" ? comparison.status === "ready" ? comparison.data.monitors.length ? <section className="monitor-comparison"><h2 className="section-title">{t("intel.monitorComparison")}</h2><p className="comparison-summary">{t("intel.comparisonSummary", { monitors: comparison.data.monitors.length, window, changes: comparison.data.monitors.reduce((sum, monitorItem) => sum + monitorItem.changed_trials, 0) })}</p><div className="table-scroll" tabIndex={0}><table className="d-table"><thead><tr><th>{t("intel.oneMonitor")}</th><th>{t("intel.currentTrials")}</th><th>{t("intel.entered")}</th><th>{t("intel.changedTrials")}</th><th>{t("intel.left")}</th><th>{t("intel.reentered")}</th><th>{t("intel.critical")}</th><th>{t("intel.important")}</th><th>{t("intel.lastRun")}</th></tr></thead><tbody>{comparison.data.monitors.map((m) => <tr key={m.monitor_id}><td><button type="button" className="text-btn" onClick={() => update({ view: "", scope: "monitor", monitor: String(m.monitor_id) })}>{m.name}</button></td><td>{m.current_trials}</td><td>{m.entered}</td><td>{m.changed_trials}</td><td>{m.left}</td><td>{m.reentered}</td><td>{m.critical}</td><td>{m.important}</td><td>{m.last_run ?? "—"}</td></tr>)}</tbody></table></div></section> : <EmptyState title={t("intel.noMonitors")} /> : comparison.status === "error" ? <EmptyState title={t("intel.loadFailed")} hint={comparison.message} /> : <Loading label={t("intel.loading")} />
      : briefing.status === "error" ? <EmptyState title={t("intel.loadFailed")} hint={briefing.message} /> : !data ? <Loading label={t("intel.loading")} /> : <>
        {data.freshness.affected.length > 0 && <div className="service-banner" role="alert"><b>{t("intel.completenessWarning")}</b><p>{data.freshness.affected.map((s) => { const why = reasonLabel(s.reason, t); return `${s.name} (${s.state}${why ? ` · ${why}` : ""})`; }).join(", ")}. {t("intel.mayBeIncomplete")}</p></div>}
        {data.freshness.complete && <p className="form-hint">{t("intel.freshComplete")}</p>}
        {data.freshness.unknown.length > 0 && <p className="form-hint">{t("intel.unknownFreshness")}: {data.freshness.unknown.map((s) => s.name).join(", ")}</p>}
        {data.freshness.disabled.length > 0 && <p className="form-hint">{t("intel.disabledSources")}: {data.freshness.disabled.map((s) => s.name).join(", ")}</p>}
        <p className="briefing-generated">{t("intel.generated", { date: (data as Briefing & { generated_at?: string }).generated_at?.slice(0, 16) ?? "—" })}</p>
        <section className="briefing-summary"><h2 className="section-title">{t("intel.executiveSummary")}</h2><p>{data.summary || t("intel.summaryTemplate", { new: data.counts.new_trials, changed: data.counts.changed_trials, events: data.counts.change_events, critical: data.counts.critical_changes, important: data.counts.important_changes, entered: data.counts.entered, left: data.counts.left })}</p><KpiStrip items={[["critical_changes", "intel.critical"], ["important_changes", "intel.important"], ["changed_trials", "intel.changedTrials"], ["new_trials", "intel.newTrials"]].map(([key, label], index) => ({ label: t(label as never), value: data.counts[key], primary: index === 0, severity: index === 0 ? "critical" : index === 1 ? "important" : undefined, onClick: () => navigate(`/updates?${new URLSearchParams(linkQuery)}${key === "critical_changes" ? "&severity=critical" : key === "important_changes" ? "&severity=important" : ""}`) }))} /></section>
        {attentionItems.length > 0 && <section className="briefing-attention"><h2 className="section-title">{t("intel.attentionList")}</h2>{attentionItems.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}</section>}
        <div className="analysis-grid briefing-overview">
          <PageSection title={t("intel.trends")}><ActivityTrend buckets={data.trends} empty={t("intel.noEvents")} t={t} showMembership /></PageSection>
          <PageSection title={t("intel.whatChanged")}><p className="form-hint briefing-cat-hint">{t("intel.category.hint")}</p><div className="briefing-category-list">{categoryRows.map(([category, count]) => <CategoryRow key={category} category={category} count={count} maxCount={maxCategoryCount} open={openCategory === category} onToggle={() => setOpenCategory((current) => current === category ? null : category)} t={t} />)}</div></PageSection>
          {openCategory && <section className="analysis-panel briefing-category-detail-panel" id="briefing-category-detail">
            <header className="analysis-panel-head"><h2>{categoryLabel(openCategory, t)} · {(data.categories[openCategory] ?? 0).toLocaleString()}</h2><button type="button" className="text-btn" onClick={() => setOpenCategory(null)} aria-label={t("a11y.close")}>×</button></header>
            {categoryDetails.status === "loading" && <Loading label={t("intel.loading")} />}
            {categoryDetails.status === "error" && <p className="form-hint">{t("intel.loadFailed")}: {categoryDetails.message}</p>}
            {categoryDetails.status === "ready" && <>
              {categoryDetails.data.items.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}
              {categoryDetails.data.total > categoryDetails.data.items.length && <button type="button" className="text-btn briefing-cat-all" onClick={() => navigate(`/updates?${new URLSearchParams({ ...linkQuery, category: openCategory })}`)}>{t("intel.category.viewAll", { n: categoryDetails.data.total })}</button>}
            </>}
          </section>}
        </div>
        {data.counts.change_events === 0 && data.counts.new_trials === 0 && data.counts.entered + data.counts.left + data.counts.reentered === 0 && <EmptyState title={t("intel.noChanges")} />}
        <h2 className="section-title detailed-findings-title">{t("intel.detailedFindings")}</h2>
        {data.sections.filter((section) => section.key !== "priority").map((section) => <section key={section.key} className="briefing-section"><h3 className="section-title">{t(`intel.section.${section.key}` as never)} <small>({section.count})</small></h3>
          {section.new_trials?.map((r) => <button type="button" className="mini-row briefing-trial-row" key={`${r.source}:${r.trial_id}`} onClick={() => navigate(`/trials/${r.source}/${r.trial_id}`)}><b className="briefing-trial-title">{r.title}</b><small>{r.source} · {r.trial_id} · {t("intel.newlyIndexed")}</small></button>)}
          {section.membership?.map((m) => <div className="mini-row" key={m.id}>{m.trial_id} · {m.monitor_name} · {m.event_type}</div>)}
          {section.groups?.map((g) => <div key={`${g.from}:${g.to}`}><h3>{g.from} → {g.to} ({g.count})</h3>{g.items.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}</div>)}
          {(section.items as EventItem[] | undefined)?.filter((r) => "event_id" in r).map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}
          {section.increases && section.increases.length > 0 && <h3>{t("intel.enrollmentIncreases")}</h3>}
          {section.increases?.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}
          {section.decreases && section.decreases.length > 0 && <h3>{t("intel.enrollmentDecreases")}</h3>}
          {section.decreases?.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}
          {section.timeline?.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}
          {section.countries?.map((item) => <EventRow item={item} t={t} lang={lang} key={item.event_id} />)}
          {section.sponsors?.slice(0, 10).map((s) => <div className="mini-row" key={s.name}><span>{s.name}</span><small>{s.new_trials} {t("intel.newTrials")} · {s.changed_trials} {t("intel.changedTrials")} · {s.priority_changes} {t("intel.important")}</small></div>)}
          {section.disagreements?.items.map((d) => <div className="mini-row" key={d.master_trial_id}><span>{d.fields.join(", ")}</span><small>{d.sources.map((s) => `${s.source}: ${s.trial_id}`).join(" · ")}</small></div>)}
          {section.sources?.map((s) => <div className="mini-row" key={s.name}><span className="mini-name">{s.name}</span><FreshnessBadge t={t} freshness={s.state} /><small>{s.data_as_of ?? s.last_success ?? "—"} UTC{reasonLabel(s.stale_reason, t) && ` · ${reasonLabel(s.stale_reason, t)}`}</small></div>)}
          {section.key === "monitor_activity" && (section.items as Member[] | undefined)?.map((m) => <div className="mini-row" key={m.id}>{m.trial_id} · {m.monitor_name} · {m.event_type}</div>)}
        </section>)}
        {data.source_activity.length > 0 && <section className="briefing-section"><h2 className="section-title">{t("intel.sourceActivity")}</h2><div className="table-scroll" tabIndex={0}><table className="d-table"><thead><tr><th>{t("intel.registry")}</th><th>{t("intel.newTrials")}</th><th>{t("intel.changedTrials")}</th><th>{t("intel.important")}</th></tr></thead><tbody>{data.source_activity.map((r) => <tr key={r.source}><td>{r.source}</td><td>{r.new_trials}</td><td>{r.changed_trials}</td><td>{r.priority_changes}</td></tr>)}</tbody></table></div></section>}
        {data.most_frequently_updated.length > 0 && <section className="briefing-section"><h2 className="section-title">{t("intel.mostFrequent")}</h2>{data.most_frequently_updated.map((r) => <button type="button" className="mini-row" key={r.trial_id} onClick={() => r.items[0] && navigate(`/trials/${r.source}/${r.trial_id}?tab=changes&event=${r.items[0].event_id}`)}><span className="mini-name">{r.title}</span><span className="mini-counts">{r.count} · {r.highest_severity}</span></button>)}</section>}
        <button type="button" className="text-btn" onClick={() => navigate(`/updates?scope=${scope}&window=${window}${scope === "monitor" ? `&monitor=${monitor}` : ""}`)}>{t("intel.viewAllChanges")}</button>
      </>}
  </main>;
}
