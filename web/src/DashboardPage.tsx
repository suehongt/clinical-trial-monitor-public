import { useJson } from "./api";
import type { Lang, Translate } from "./i18n";
import type { BackendHealth } from "./health";
import type { DashboardData, ScopedUpdateItem } from "./types";
import { navigate, useRoute } from "./router";
import { isProjectList } from "./projectApi";
import { ActivityTrend, CategoryBreakdown, ChangeRow, FreshnessSummary, IntelligenceMetricStrip, PageSection, type TrendBucket } from "./IntelligenceViz";
import ScopeHeader from "./ScopeHeader";
import { EmptyState, Loading, ServiceUnavailable } from "./ui";
import { parseIntelligenceFilters, sortBySeverity } from "./intelligenceState";

type Intelligence = {
  generated_at?: string; counts: Record<string, number>; categories: Record<string, number>; trends: TrendBucket[];
  priority_events: Array<Partial<ScopedUpdateItem> & Pick<ScopedUpdateItem, "event_id" | "trial_id" | "source" | "title" | "field_name" | "severity" | "detected_at">>;
  freshness: { sources: Array<{ full_name: string; freshness: string; last_successful_sync?: string | null; stale_reason?: string }> };
};
const isIntelligence = (v: unknown): v is Intelligence => typeof v === "object" && v !== null && typeof (v as Intelligence).counts === "object" && Array.isArray((v as Intelligence).trends);
const isDashboard = (v: unknown): v is DashboardData => typeof v === "object" && v !== null && typeof (v as DashboardData).summary === "object" && Array.isArray((v as DashboardData).monitors);

export default function DashboardPage({ t, lang, health }: { t: Translate; lang: Lang; health: BackendHealth }) {
  const route = useRoute(); const filters = parseIntelligenceFilters(route.query, false);
  const { scope, window, projectId, monitorId } = filters;
  const projects = useJson(health.state === "online" ? "/api/projects" : null, isProjectList);
  const dash = useJson<DashboardData>(health.state === "online" ? `/api/dashboard?window=${window}&limit=5` : null, isDashboard);
  const apiParams = new URLSearchParams({ scope, window });
  if (scope === "project" && projectId) apiParams.set("project_id", projectId);
  if (scope === "monitor" && monitorId) apiParams.set("monitor_id", monitorId);
  const linkParams = new URLSearchParams({ scope, window });
  if (scope === "project" && projectId) linkParams.set("project_id", projectId);
  if (scope === "monitor" && monitorId) linkParams.set("monitor", monitorId);
  const intelligence = useJson<Intelligence>(health.state === "online" ? `/api/intelligence/overview?${apiParams}` : null, isIntelligence);
  const update = (part: Record<string, string>) => { const next = new URLSearchParams(route.query); Object.entries(part).forEach(([k, v]) => v ? next.set(k, v) : next.delete(k)); navigate(`/dashboard?${next}`); };
  const choices = [
    { value: "monitored", label: t("intel.allMonitored") },
    ...(projects.status === "ready" ? projects.data.projects.map((p) => ({ value: `project:${p.id}`, label: `${t("ws.project")} · ${p.name}` })) : []),
    ...(dash.status === "ready" ? dash.data.monitors.map((m) => ({ value: `monitor:${m.id}`, label: `${t("ws.monitor")} · ${m.name}` })) : []),
    { value: "watched", label: t("nav.watched") }, { value: "all", label: t("ws.allIndexedScope") },
  ];
  const selected = scope === "project" ? `project:${projectId}` : scope === "monitor" ? `monitor:${monitorId}` : scope;
  if (!choices.some((choice) => choice.value === selected)) choices.splice(1, 0, { value: selected, label: scope === "project" ? `${t("ws.project")} #${projectId}` : `${t("ws.monitor")} #${monitorId}` });
  const scopeTitle = choices.find((choice) => choice.value === selected)?.label.replace(/^(Project|Monitor|项目|监测器) · /, "") ?? t("intel.allMonitored");
  if (health.state !== "online") return <main className="page-main"><ServiceUnavailable health={health} t={t} /></main>;
  const data = intelligence.status === "ready" ? intelligence.data : null; const counts = data?.counts ?? {}; const sources = data?.freshness.sources ?? [];
  const updatesUrl = `/updates?${linkParams}`; const priority = data ? sortBySeverity(data.priority_events).slice(0, 8).map((item) => ({ ...item, old_value: item.old_value ?? null, new_value: item.new_value ?? null, watched: item.watched ?? false, monitors: item.monitors ?? [], project_trial: item.project_trial ?? false, project_monitors: item.project_monitors ?? [] }) as ScopedUpdateItem) : [];
  return <main className="page-main dashboard-page intelligence-page">
    <ScopeHeader t={t} eyebrow={t("ws.dashboardEyebrow")} title={scopeTitle} description={t("ws.dashboardDescription")} meta={data ? `${t("ws.dashboardMeta", { trials: counts.current_trials ?? 0, changes: counts.change_events ?? 0, window })} · ${t("intel.generated", { date: data.generated_at?.slice(0, 16) ?? "—" })}` : undefined} choices={choices} selected={selected} onSelect={(value) => {
      if (value.startsWith("project:")) update({ scope: "project", project_id: value.split(":")[1], monitor_id: "", monitor: "" });
      else if (value.startsWith("monitor:")) update({ scope: "monitor", monitor_id: value.split(":")[1], project_id: "", monitor: "" });
      else update({ scope: value, project_id: "", monitor_id: "", monitor: "" });
    }}><select aria-label={t("ws.time")} value={window} onChange={(event) => update({ window: event.target.value })}><option value="24h">{t("ws.hours24")}</option><option value="7d">{t("ws.days7")}</option><option value="30d">{t("ws.days30")}</option></select></ScopeHeader>
    {intelligence.status === "error" && <EmptyState title={t("dash.loadFailed")} hint={intelligence.message} />}
    {!data && intelligence.status !== "error" && <div aria-busy="true"><Loading label={t("loading.dashboard")} /></div>}
    {data && <>
      <IntelligenceMetricStrip items={[
        { label: t("ws.criticalChanges"), value: counts.critical_changes ?? 0, severity: "critical", primary: true, onClick: () => navigate(`${updatesUrl}&severity=critical`) },
        { label: t("ws.importantChanges"), value: counts.important_changes ?? 0, severity: "important", onClick: () => navigate(`${updatesUrl}&severity=important`) },
        { label: t("ws.changedTrials"), value: counts.changed_trials ?? 0, note: t("ws.currentTrialNote", { n: counts.change_events ?? 0 }), onClick: () => navigate(updatesUrl) },
        { label: t("ws.newEntered"), value: (counts.new_trials ?? 0) + (counts.entered ?? 0), onClick: () => navigate(updatesUrl) },
      ]} />
      <FreshnessSummary sources={sources} t={t} />
      <PageSection className="priority-panel" title={t("intel.priorityChanges")} action={<button className="text-btn" onClick={() => navigate(`${updatesUrl}&severity=priority`)}>{t("ws.viewAll")} →</button>}>
        {priority.length === 0 ? <EmptyState title={t("ws.noPriority")} /> : <div className="intel-change-list">{priority.map((item) => <ChangeRow key={item.event_id} item={item} t={t} lang={lang} />)}</div>}
      </PageSection>
      <div className="analysis-grid"><PageSection title={t("ws.trend")}><ActivityTrend t={t} buckets={data.trends} empty={t("ws.activity.empty")} /></PageSection><PageSection title={t("ws.whatChanged")} action={<button className="text-btn" onClick={() => navigate(`/briefing?${linkParams}`)}>{t("intel.briefing")}</button>}><CategoryBreakdown t={t} categories={data.categories} empty={t("ws.category.empty")} onSelect={(category) => navigate(`${updatesUrl}&category=${encodeURIComponent(category)}`)} /></PageSection></div>
      <div className="analysis-grid secondary-intelligence"><PageSection title={t("ws.researchActivity")}>
        {dash.status === "ready" && dash.data.monitors.slice(0, 4).map((monitor) => <button className="analysis-row" key={monitor.id} onClick={() => navigate(`/monitors/${monitor.id}`)}><strong>{monitor.name}</strong><small>{t("ws.monitorActivityCount", { entered: monitor.new_count, changed: monitor.changed_count })}</small></button>)}
        {dash.status === "ready" && dash.data.monitors.length === 0 && <EmptyState title={t("dash.noMonitors")} />}
      </PageSection><PageSection title={t("nav.projects")} action={<button className="text-btn" onClick={() => navigate("/projects")}>{t("ws.viewAll")} →</button>}>
        {projects.status === "ready" && projects.data.projects.slice(0, 4).map((project) => <button className="analysis-row" key={project.id} onClick={() => navigate(`/projects/${project.id}`)}><strong>{project.name}</strong><small>{t("ws.projectCardPriority", { important: project.recent_important_changes ?? 0, critical: 0 })}</small></button>)}
        {projects.status === "ready" && projects.data.projects.length === 0 && <EmptyState title={t("ws.noProjects")} />}
      </PageSection></div>
    </>}
  </main>;
}
