/**
 * Updates (Phase 3E): one top-level area aggregating everything that
 * changed.  Subviews via ?view=…:
 *
 *   all           — meaningful change events across trials (from the
 *                   persisted events stream), severity + watched flags,
 *                   Important-only filter; clicks land on Trial → Changes
 *   notifications — durable in-app notification feed (read state intact;
 *                   the underlying models stay separate per requirement)
 *   digest        — the existing daily digest (day groups + new trials)
 *
 * Legacy /changes and /notifications routes redirect here.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { refreshableUrl, sendJson, useJson, useStickyJson } from "./api";
import type { DayNewTrial, EventsData, NotificationRow, ScopedUpdatesData } from "./types";
import { isEventsData } from "./guards";
import {
  getUnreadCount, isMonitorListPayload, isNotificationList, isScopedUpdates,
  markAllNotificationsRead, markNotificationRead,
} from "./monitoringApi";
import { SOURCE_BADGES } from "./labels";
import { Drawer, EmptyState, Loading, relTime } from "./ui";
import type { Lang, Translate } from "./i18n";
import type { BackendHealth } from "./health";
import { ServiceUnavailable } from "./ui";
import { navigate, useRoute } from "./router";
import AddToProject from "./AddToProject";
import { isProjectList } from "./projectApi";
import ScopeHeader from "./ScopeHeader";
import { ActivityTrend, CategoryBreakdown, ChangeRow, categoryLabel, IntelligenceMetricStrip, PageSection, RegistryBadge } from "./IntelligenceViz";
import { safeExternalUrl } from "./urlSafety";
import { clearUpdateFilters, groupUpdates, notificationRows, parseIntelligenceFilters, updateNotificationRead } from "./intelligenceState";

type View = "all" | "notifications" | "digest";
const VIEWS: View[] = ["all", "notifications", "digest"];

function toView(v: string | null): View {
  return VIEWS.includes(v as View) ? (v as View) : "all";
}

export default function UpdatesPage({ t, lang, health, onUnreadCount }: {
  t: Translate; lang: Lang; health: BackendHealth;
  onUnreadCount?: (n: number) => void;
}) {
  const route = useRoute();
  const view = toView(route.query.get("view"));
  const online = health.state === "online";

  const selectView = (next: View) => {
    const query = new URLSearchParams(route.query);
    if (next === "all") query.delete("view"); else query.set("view", next);
    navigate(`/updates?${query}`, true);
  };

  return (
    <main className="page-main intelligence-page updates-page">
      <div className="page-head">
        <div>
          <h1>{view === "notifications" ? t("nav.notifications") : view === "digest" ? t("updates.tab.digest") : t("nav.updates")}</h1>
          <p className="page-sub">{t("updates.sub")}</p>
        </div>
      </div>

      <div className="page-tabs" role="tablist" aria-label={t("a11y.updateViews")}>
        {VIEWS.map((v) => (
          <button type="button" key={v} role="tab" aria-selected={view === v}
                  className={`ptab ${view === v ? "ptab-on" : ""}`} onClick={() => selectView(v)}>
            {t(`updates.tab.${v}` as never)}
          </button>
        ))}
        <button type="button" role="tab" aria-selected={false} className="ptab" onClick={() => navigate("/briefing")}>{t("intel.briefing")}</button>
      </div>

      {!online ? (
        <ServiceUnavailable health={health} t={t} />
      ) : view === "all" ? (
        <AllUpdates t={t} lang={lang} />
      ) : view === "notifications" ? (
        <NotificationsPane t={t} lang={lang} onUnreadCount={onUnreadCount} />
      ) : (
        <DailyDigest t={t} lang={lang} />
      )}
    </main>
  );
}

/* ── All updates ──────────────────────────────────────────────────────── */

function AllUpdates({ t, lang }: { t: Translate; lang: Lang }) {
  const route = useRoute();
  const filters = parseIntelligenceFilters(route.query);
  const { scope, window, registry, severity, category, monitorId: monitor, projectId: project, page } = filters;
  const [filterOpen, setFilterOpen] = useState(false);
  const filterButtonRef = useRef<HTMLButtonElement>(null);
  const projects = useJson("/api/projects", isProjectList);
  const params = useMemo(() => new URLSearchParams({ scope, window, page: String(page), ...(registry ? { registry } : {}), ...(severity ? { severity } : {}), ...(category ? { category } : {}), ...(scope === "monitor" && monitor ? { monitor_id: monitor } : {}), ...(scope === "project" && project ? { project_id: project } : {}) }), [scope, window, registry, severity, category, monitor, project, page]);
  // both reads go through the shared json cache: returning to this page (or
  // re-opening the same filter combination) repaints instantly and
  // revalidates in the background; a new filter combination loads fresh
  const monitorsList = useJson<{ monitors: Array<{ id: number; name: string }> }>("/api/monitors", isMonitorListPayload);
  const monitors = monitorsList.status === "ready" ? monitorsList.data.monitors : [];
  const updates = useJson<ScopedUpdatesData>(`/api/updates?${params.toString()}`, isScopedUpdates);
  const data = updates.status === "ready" ? updates.data : null;
  const error = updates.status === "error" ? updates.message : null;
  const sourceHealth = useJson<{ registries: Array<{ freshness: string; full_name: string }> }>("/api/registries/status",
    (v): v is { registries: Array<{ freshness: string; full_name: string }> } => typeof v === "object" && v !== null && Array.isArray((v as { registries?: unknown }).registries));
  const update = (part: Record<string, string>) => {
    const next = new URLSearchParams(route.query);
    Object.entries(part).forEach(([k, v]) => v ? next.set(k, v) : next.delete(k));
    if (!("page" in part)) next.delete("page");
    if (part.scope && part.scope !== "monitor") { next.delete("monitor"); next.delete("monitor_id"); }
    if (part.scope && part.scope !== "project") next.delete("project_id");
    navigate(`/updates?${next.toString()}`, true);
  };
  if (error) return <EmptyState title={t("updates.loadFailed")} hint={error} />;
  if (!data) return <Loading label={t("loading.events")} />;
  const heading = scope === "watched" ? t("ws.watchedUpdates") : scope === "monitor" ? t("ws.monitorUpdates", { name: data.monitor_name ?? t("ws.monitor") }) : scope === "project" ? t("ws.projectUpdates", { name: projects.status === "ready" ? projects.data.projects.find((p) => String(p.id) === project)?.name ?? t("ws.project") : t("ws.project") }) : scope === "all" ? t("ws.allIndexedUpdates") : t("ws.allMonitoredTitle");
  const delayed = sourceHealth.status === "ready" ? sourceHealth.data.registries.filter((s) => s.freshness === "stale" || s.freshness === "delayed") : [];
  const groups = groupUpdates(data.items);
  const activeFilters = [["registry", registry], ["severity", severity], ["category", category]].filter((entry): entry is [string, string] => Boolean(entry[1]));
  const controls = <div className="update-filter-controls">
    <label><span>{t("ws.updateScope")}</span><select aria-label={t("ws.updateScope")} value={scope} onChange={(e) => update(e.target.value === "monitor" ? { scope: "monitor", monitor: monitor || String(monitors[0]?.id ?? "") } : e.target.value === "project" ? { scope: "project", project_id: project || String(projects.status === "ready" ? projects.data.projects[0]?.id ?? "" : "") } : { scope: e.target.value })}><option value="monitored">{t("intel.allMonitored")}</option><option value="watched">{t("nav.watched")}</option><option value="all">{t("ws.allIndexed")}</option><option value="monitor">{t("ws.oneMonitor")}</option><option value="project">{t("ws.oneProject")}</option></select></label>
    {scope === "monitor" && <label><span>{t("ws.monitor")}</span><select aria-label={t("ws.monitor")} value={monitor} onChange={(e) => update({ monitor: e.target.value })}>{monitors.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>}
    {scope === "project" && <label><span>{t("ws.project")}</span><select aria-label={t("ws.project")} value={project} onChange={(e) => update({ project_id: e.target.value })}>{projects.status === "ready" && projects.data.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
    <label><span>{t("ws.time")}</span><select aria-label={t("ws.time")} value={window} onChange={(e) => update({ window: e.target.value })}><option value="24h">{t("ws.hours24")}</option><option value="7d">{t("ws.days7")}</option><option value="30d">{t("ws.days30")}</option><option value="all">{t("ws.allTime")}</option></select></label>
    <label><span>{t("ws.registry")}</span><select aria-label={t("ws.registry")} value={registry} onChange={(e) => update({ registry: e.target.value })}><option value="">{t("intel.allRegistries")}</option>{Object.keys(data.facets.registries).map((name) => <option key={name} value={name}>{SOURCE_BADGES[name]?.label ?? name} ({data.facets.registries[name]})</option>)}</select></label>
    <label><span>{t("ws.severity")}</span><select aria-label={t("ws.severity")} value={severity} onChange={(e) => update({ severity: e.target.value })}><option value="">{t("ws.allSeverity")}</option><option value="priority">{t("ws.prioritySeverity")}</option>{["critical", "important", "normal", "minor"].map((name) => <option key={name} value={name}>{t(`sev.${name}` as never)} ({data.facets.severities[name] ?? 0})</option>)}</select></label>
    <label><span>{t("ws.category")}</span><select aria-label={t("ws.category")} value={category} onChange={(e) => update({ category: e.target.value })}><option value="">{t("ws.allCategories")}</option>{Array.from(new Set(["recruitment", "timeline", "outcomes", "enrollment", "sponsor", "geography", "intervention", "eligibility", "other", ...Object.keys(data.analytics?.categories ?? {}), ...(category ? [category] : [])])).map((name) => <option key={name} value={name}>{categoryLabel(name, t)}</option>)}</select></label>
  </div>;
  return <>
    <ScopeHeader t={t} eyebrow={t("ws.changeAnalysis")} title={heading} meta={t("ws.updatesMeta", { changes: data.total.toLocaleString(), trials: data.trial_count.toLocaleString(), window: window === "all" ? t("ws.allTime") : window })} />
    {delayed.length > 0 && <p className="form-hint">{t("ws.updateDelayed", { sources: delayed.map((s) => s.full_name).join(", ") })} <button className="text-btn" type="button" onClick={() => navigate("/data-sources")}>{t("ws.viewSources")}</button></p>}
    <div className="updates-filter-toolbar"><button ref={filterButtonRef} type="button" className="ui-button ui-button-secondary mobile-filter-button" onClick={() => setFilterOpen(true)}>{t("updates.openFilters")}</button><div className="desktop-update-filters">{controls}</div></div>
    <Drawer open={filterOpen} label={t("updates.filters")} onClose={() => setFilterOpen(false)} triggerRef={filterButtonRef}><div className="update-filter-drawer"><header><h2>{t("updates.filters")}</h2><button className="text-btn" onClick={() => setFilterOpen(false)}>{t("a11y.close")}</button></header>{controls}</div></Drawer>
    {activeFilters.length > 0 && <div className="active-filter-row" aria-label={t("updates.activeFilters")}>{activeFilters.map(([key, value]) => <button type="button" className="filter-chip" key={key} onClick={() => update({ [key]: "" })}>{key}: {key === "category" ? categoryLabel(value, t) : value}<span aria-hidden="true"> ×</span></button>)}<button type="button" className="text-btn" onClick={() => navigate(`/updates?${clearUpdateFilters(route.query)}`, true)}>{t("updates.clearFilters")}</button></div>}
    {data.analytics && <>
      <IntelligenceMetricStrip items={[
        { label: t("ws.changeEvents"), value: data.total },
        { label: t("ws.changedTrials"), value: data.trial_count },
        { label: t("sev.important"), value: data.facets.severities.important ?? 0 },
        { label: t("sev.critical"), value: data.facets.severities.critical ?? 0 },
      ]} />
      <div className="analysis-grid"><PageSection title={t("ws.trend")}><ActivityTrend t={t} buckets={data.analytics.trends} empty={t("ws.trend.empty")} /></PageSection><PageSection title={t("ws.categories")}><CategoryBreakdown t={t} categories={data.analytics.categories} empty={t("ws.category.empty")} onSelect={(value) => update({ category: value })} /></PageSection></div>
    </>}
    <h2 className="section-title">{t("ws.detailed")}</h2>
    {data.items.length === 0 && <EmptyState title={t("ws.noFilteredUpdates")}><button className="text-btn" onClick={() => navigate(`/updates?${clearUpdateFilters(route.query)}`)}>{t("updates.clearFilters")}</button></EmptyState>}
    <div className="date-change-stream">{groups.map((dateGroup) => <section className="date-change-group" key={dateGroup.date} aria-labelledby={`date-${dateGroup.date}`}><h3 id={`date-${dateGroup.date}`}>{dateGroup.date}</h3>{dateGroup.trials.map((trial) => <section className="trial-change-group" key={trial.key}><header><div><RegistryBadge registry={trial.source} /><strong>{trial.title}</strong><span className="tid">{trial.trialId}</span></div><span>{t("updates.changeCount", { n: trial.items.length })}</span></header><div className="intel-change-list">{trial.items.map((item) => <ChangeRow key={item.event_id} item={item} t={t} lang={lang} addon={<AddToProject kind="evidence" eventId={item.event_id} />} />)}</div></section>)}</section>)}</div>
    {data.total > data.page_size && <nav className="updates-pagination" aria-label={t("ws.pagination", { page })}><button type="button" disabled={page <= 1} onClick={() => update({ page: String(page - 1) })}>{t("updates.previousPage")}</button><span>{t("updates.page", { page })}</span><button type="button" disabled={page * data.page_size >= data.total} onClick={() => update({ page: String(page + 1) })}>{t("updates.nextPage")}</button></nav>}
  </>;
}

/* ── Notifications (durable in-app model, read state preserved) ───────── */

function NotificationsPane({ t, lang, onUnreadCount }: {
  t: Translate; lang: Lang; onUnreadCount?: (n: number) => void;
}) {
  void lang;
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [tick, setTick] = useState(0);
  const feed = useStickyJson<{ notifications: NotificationRow[] }>(
    refreshableUrl(`/api/notifications?limit=200${unreadOnly ? "&unread=true" : ""}`, tick), isNotificationList);
  const [localRows, setLocalRows] = useState<NotificationRow[] | null>(null);
  useEffect(() => { if (feed.data) setLocalRows(feed.data.notifications); }, [feed.data]);
  const rows = localRows;
  const error = feed.error;
  const [unread, setUnread] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);

  // unread count is tiny and must stay fresh after read actions, so it sits
  // beside the cached feed instead of inside it
  useEffect(() => {
    let alive = true;
    getUnreadCount()
      .then((c) => { if (alive) { setUnread(c.count); onUnreadCount?.(c.count); } })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [tick, unreadOnly, onUnreadCount]);

  const afterAction = () => setTick((x) => x + 1);
  const markRead = (n: NotificationRow, read = true) => {
    if (!rows) return;
    const previous = rows; setActionError(null); setLocalRows(updateNotificationRead(rows, n.id, read, new Date().toISOString()));
    const nextUnread = Math.max(0, unread + (read && !n.read_at ? -1 : !read && n.read_at ? 1 : 0));
    setUnread(nextUnread);
    markNotificationRead(n.id, read).then(() => { onUnreadCount?.(nextUnread); afterAction(); }).catch(() => { setLocalRows(previous); setActionError(t("updates.actionFailed")); setTick((x) => x + 1); });
  };

  if (error) return <EmptyState title={t("updates.notifLoadFailed")} hint={error} />;
  if (!rows) return <Loading label={t("loading.notifications")} />;

  return (
    <>
      <div className="toolbar toolbar-static">
        <div className="range-chips" role="group" aria-label={t("a11y.updateViews")}>
          <button type="button" className={`chip ${!unreadOnly ? "chip-on" : ""}`} aria-pressed={!unreadOnly}
                  onClick={() => setUnreadOnly(false)}>{t("updates.allNotifs")}</button>
          <button type="button" className={`chip ${unreadOnly ? "chip-on" : ""}`} aria-pressed={unreadOnly}
                  onClick={() => setUnreadOnly(true)}>{t("updates.unreadNotifs")}</button>
        </div>
        <div className="range-chips">
          <span className="pill tone-blue">{t("updates.unreadBadge", { n: unread })}</span>
          <button type="button" className="chip" disabled={unread === 0} onClick={() => { const previous = rows; setActionError(null); setLocalRows(rows.map((row) => ({ ...row, read_at: row.read_at ?? new Date().toISOString() }))); setUnread(0); markAllNotificationsRead().then(() => { onUnreadCount?.(0); afterAction(); }).catch(() => { setLocalRows(previous); setActionError(t("updates.actionFailed")); setTick((x) => x + 1); }); }}>
            {t("updates.markAllRead")}
          </button>
        </div>
      </div>

      {actionError && <div className="error-note" role="alert">{actionError}</div>}
      {notificationRows(rows, unreadOnly).length === 0 && <EmptyState title={t("updates.noNotifs")} hint={t("updates.noNotifsHint")} />}

      <div className="notification-list">
        {notificationRows(rows, unreadOnly).map((n) => (
          <article className={`notification-row ${!n.read_at ? "notification-unread" : ""}`} key={n.id} aria-label={!n.read_at ? `${t("updates.unreadNotifs")}: ${n.trial_title || n.summary || n.event_type}` : undefined}>
            {!n.read_at && <span className="unread-indicator"><span aria-hidden="true" />{t("updates.unreadNotifs")}</span>}
            <span className={`pill ${n.event_type === "trial_left" ? "tone-gray" : "tone-blue"}`}>
              {n.event_type.replace("trial_", "")}
            </span>
            <div className="change-main">
              {n.trial_id ? (
                <button type="button" className="text-btn update-title"
                        onClick={() => {
                          if (!n.read_at) markRead(n);
                          navigate(`/trials/${encodeURIComponent(n.source || "NCT")}/${encodeURIComponent(n.trial_id!)}?tab=changes${n.trial_event_id ? `&event=${n.trial_event_id}` : ""}`);
                        }}>
                  {n.trial_title || n.trial_id}
                </button>
              ) : <b>{n.summary || n.event_type}</b>}
              {n.trial_id && n.summary && <span>{n.summary}</span>}
              <small className="change-meta">
                {n.monitor_name || "Monitor"} · <span title={n.created_at}>{relTime(n.created_at)}</span>
                {n.email_status ? ` · Email: ${n.email_status}` : ""}
              </small>
            </div>
            <button type="button" className="text-btn" onClick={() => markRead(n, !n.read_at)}>{n.read_at ? t("updates.markUnread") : t("updates.markRead")}</button>
            {n.email_status === "failed" && n.email_notification_id && (
              <button type="button" className="text-btn" onClick={() => {
                fetch(`/api/notifications/${n.email_notification_id}/retry-email`, { method: "POST" })
                  .then(afterAction).catch(() => undefined);
              }}>{t("updates.retryEmail")}</button>
            )}
          </article>
        ))}
      </div>
    </>
  );
}

/* ── Daily digest (existing day-group view, preserved semantics) ──────── */

function DailyDigest({ t, lang }: { t: Translate; lang: Lang }) {
  const route = useRoute();
  const filters = parseIntelligenceFilters(route.query);
  const range = filters.window === "30d" ? 30 : filters.window === "all" ? 0 : 7;
  const setRange = (next: 7 | 30 | 0) => { const query = new URLSearchParams(route.query); query.set("view", "digest"); query.set("window", next === 30 ? "30d" : next === 0 ? "all" : "7d"); navigate(`/updates?${query}`, true); };
  const scopedParams = new URLSearchParams({ scope: filters.scope, window: filters.window, page_size: "100" });
  if (filters.scope === "monitor" && filters.monitorId) scopedParams.set("monitor_id", filters.monitorId);
  if (filters.scope === "project" && filters.projectId) scopedParams.set("project_id", filters.projectId);
  if (filters.registry) scopedParams.set("registry", filters.registry);
  const scoped = useJson<ScopedUpdatesData>(`/api/updates?${scopedParams}`, isScopedUpdates);
  const eventsState = useJson<EventsData>(filters.scope === "all" ? `/api/events?days=${range}&limit=0` : null, isEventsData);
  if (scoped.status === "error") return <EmptyState title={t("updates.digestLoadFailed")} hint={scoped.message} />;
  if (scoped.status !== "ready") return <Loading label={t("loading.events")} />;
  const groups = groupUpdates(scoped.data.items); const daily = eventsState.status === "ready" ? eventsState.data.daily ?? [] : [];
  return (
    <>
      <section className="digest-head"><div><span className="scope-eyebrow">{filters.scope} · {filters.window}</span><h2>{t("updates.digestTitle")}</h2><p>{t("updates.digestDescription")}</p></div><div><button className="chip" type="button" onClick={() => globalThis.print()}>{t("intel.print")}</button><button className="chip" type="button" onClick={() => { const query = new URLSearchParams(route.query); query.delete("view"); navigate(`/updates?${query}`); }}>{t("intel.viewUpdates")}</button></div></section>
      <WatchKeywords t={t} />
      <div className="range-chips" role="group" aria-label={t("a11y.range")}>
        {([7, 30, 0] as const).map((r) => (
          <button type="button" key={r} className={`chip ${range === r ? "chip-on" : ""}`}
                  aria-pressed={range === r} onClick={() => setRange(r)}>
            {r === 7 ? t("range.7") : r === 30 ? t("range.30") : t("range.all")}
          </button>
        ))}
      </div>
      <IntelligenceMetricStrip items={[{ label: t("ws.changeEvents"), value: scoped.data.total }, { label: t("ws.changedTrials"), value: scoped.data.trial_count }, { label: t("sev.critical"), value: scoped.data.facets.severities.critical ?? 0, severity: "critical" }, { label: t("sev.important"), value: scoped.data.facets.severities.important ?? 0, severity: "important" }]} />
      {scoped.data.analytics && <PageSection title={t("ws.categories")}><CategoryBreakdown categories={scoped.data.analytics.categories} empty={t("ws.category.empty")} t={t} /></PageSection>}
      {groups.length === 0 && <EmptyState title={t("timeline.empty")} />}
      <div className="date-change-stream digest-change-stream">{groups.map((dateGroup) => <section className="date-change-group" key={dateGroup.date}><h3>{dateGroup.date}</h3>{dateGroup.trials.map((trial) => <section className="trial-change-group" key={trial.key}><header><div><RegistryBadge registry={trial.source} /><strong>{trial.title}</strong><span className="tid">{trial.trialId}</span></div><span>{t("updates.changeCount", { n: trial.items.length })}</span></header><div className="intel-change-list">{trial.items.map((item) => <ChangeRow key={item.event_id} item={item} t={t} lang={lang} />)}</div></section>)}</section>)}</div>
      {filters.scope === "all" && daily.some((day) => day.new_trials.length > 0) && <section className="digest-new-trials"><h2 className="section-title">{t("day.newSection")}</h2>{daily.flatMap((day) => day.new_trials).map((trial: DayNewTrial) => <div className="nt-row" key={trial.source + trial.id}><RegistryBadge registry={trial.source} /><button type="button" className="tc-title tc-open" onClick={() => navigate(`/trials/${encodeURIComponent(trial.source)}/${encodeURIComponent(trial.id)}`)}>{trial.title}</button><span className="tid">{trial.id}</span>{safeExternalUrl(trial.url) && <a className="tc-link" href={safeExternalUrl(trial.url)!} target="_blank" rel="noreferrer">{t("link.registry")}</a>}</div>)}</section>}
    </>
  );
}

/* ── Watch keywords (preserved from the old digest view) ─────────────── */

interface WatchInfoRow {
  watch_id: number; keyword: string; created_at: string;
  total_matches: number; recent_hits: number;
}

const isWatchInfoList = (v: unknown): v is { watches: WatchInfoRow[] } =>
  typeof v === "object" && v !== null && Array.isArray((v as { watches?: unknown }).watches);

function WatchKeywords({ t }: { t: Translate }) {
  const [tick, setTick] = useState(0);
  const [input, setInput] = useState("");
  const [err, setErr] = useState<string | null>(null);
  // cached GET: the digest view re-renders this card on every visit without
  // re-fetching it visibly; add/remove bump the tick for a fresh read
  const watchState = useStickyJson<{ watches: WatchInfoRow[] }>(
    refreshableUrl("/api/watch", tick), isWatchInfoList);
  const rows = watchState.data?.watches ?? null;

  const add = () => {
    const kw = input.trim();
    if (!kw) return;
    setErr(null);
    sendJson<unknown>(`/api/watch?keyword=${encodeURIComponent(kw)}`, "POST",
      (v): v is unknown => typeof v === "object" && v !== null)
      .then(() => { setInput(""); setTick((x) => x + 1); })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  };

  if (rows === null) return null;
  return (
    <div className="watch-card" role="region" aria-label={t("a11y.watch")}>
      <div className="watch-head">
        <span className="watch-title">{t("watch.title")}</span>
        <span className="watch-hint">{t("watch.hint")}</span>
      </div>
      <div className="watch-add">
        <input className="watch-input" placeholder={t("watch.placeholder")} value={input}
               onChange={(e) => setInput(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button type="button" className="chip live-btn" onClick={add}>{t("watch.add")}</button>
      </div>
      {err && <div className="error-note" role="alert">{err}</div>}
      {rows.length === 0 && <div className="empty-hint">{t("watch.empty")}</div>}
      {rows.map((w) => (
        <div className="watch-row" key={w.watch_id}>
          <div className="watch-kw">
            <span className="watch-keyword">{w.keyword}</span>
            <span className="watch-counts">
              {t("watch.total", { n: w.total_matches })} · {t("watch.hits", { n: w.recent_hits })}
            </span>
            <button type="button" className="watch-del" aria-label={`${t("updates.markRead")} ${w.keyword}`}
                    onClick={() => {
                      sendJson<unknown>(`/api/watch/${w.watch_id}`, "DELETE",
                        (v): v is unknown => typeof v === "object" && v !== null)
                        .then(() => setTick((x) => x + 1)).catch(() => undefined);
                    }}>✕</button>
          </div>
        </div>
      ))}
    </div>
  );
}
