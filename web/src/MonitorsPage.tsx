/**
 * Monitors (Phase 3E): topic-monitor workspace.
 *
 *   /monitors          list — name, active/paused, schedule, matched counts
 *                      (current/new/changed/left), latest + next run
 *   /monitors/:id      detail — Overview (readable rules + edit), Trials
 *                      (matched trials linking to trial detail), Activity
 *                      (change events linking to the trial changes),
 *                      Runs (execution history)
 *
 * Existing monitor semantics (entered/left/reentered, scheduler, run
 * idempotency) are preserved — this is presentation only.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { refreshableUrl, useStickyJson } from "./api";
import {
  createMonitor, isMonitorActivityPayload, isMonitorListPayload, isMonitorPayload,
  isMonitorRunsPayload, isMonitorTrialsPayload, runMonitor, updateMonitor,
} from "./monitoringApi";
import { SOURCE_BADGES, statusTone } from "./labels";
import { fieldLabel } from "./fieldLabels";
import { Button, ChangeValue, Drawer, EmptyState, InlineAlert, Loading, SeverityBadge, TrialLink, absTime, relTime } from "./ui";
import type {
  MonitorActivityRow, MonitorRunRow, MonitorSummary, MonitorTrialRow,
} from "./types";
import type { Lang, Translate } from "./i18n";
import type { BackendHealth } from "./health";
import { ServiceUnavailable } from "./ui";
import { navigate, useRoute } from "./router";
import { monitorRuleToSearchState, searchStateToQuery } from "./searchState";
import { inputValueToUtcStamp, utcStampToInputValue } from "./monitorTime";
import AddToProject from "./AddToProject";
import { KpiStrip, MonitorStatusBadge } from "./IntelligenceViz";
import { RULE_FIELDS, draftToRule, filterMonitors, monitorScheduleFilter, monitorStatusFilter, ruleToText, type Rules } from "./phase4State";

/**
 * Rule values are string[] or plain strings depending on whether the monitor
 * was created free-form or tracked from a search — both match identically in
 * core/monitors.py.  Display joins; editing round-trips through comma-split
 * so a multi-term rule never silently collapses into one bogus term.
 */
type ScheduleFrequency = "manual" | "hourly" | "daily" | "weekly";

export default function MonitorsPage({ t, lang, health }: { t: Translate; lang: Lang; health: BackendHealth }) {
  const route = useRoute();
  const detailId = route.segments.length > 1 ? Number(route.segments[1]) : null;

  if (health.state !== "online") {
    return <main className="page-main"><ServiceUnavailable health={health} t={t} /></main>;
  }
  if (detailId !== null && Number.isFinite(detailId)) {
    // key resets all per-monitor state (incl. the cached-read mirror) so a
    // different monitor never repaints the previous monitor's rows
    return <MonitorDetailPage key={detailId} id={detailId} t={t} lang={lang} health={health} />;
  }
  return <MonitorListPage t={t} lang={lang} />;
}

/* ── list ─────────────────────────────────────────────────────────────── */

function MonitorListPage({ t }: { t: Translate; lang: Lang }) {
  const route = useRoute();
  const [showCreate, setShowCreate] = useState(false);
  const createTrigger = useRef<HTMLButtonElement>(null);
  const [tick, setTick] = useState(0);
  const [scheduleBusy, setScheduleBusy] = useState<number | null>(null);
  const [scheduleErr, setScheduleErr] = useState("");
  // next-run pinning: one row at a time opens a datetime-local editor; the
  // draft is viewer-local and converts to the scheduler's UTC stamp on save
  const [editRunFor, setEditRunFor] = useState<number | null>(null);
  const [runDraft, setRunDraft] = useState("");
  // cached read: the list repaints instantly on revisit and revalidates
  // quietly; create/schedule changes bump the tick for a fresh read while
  // the sticky hook keeps the current table visible meanwhile
  const monitorsState = useStickyJson<{ monitors: MonitorSummary[] }>(
    refreshableUrl("/api/monitors", tick), isMonitorListPayload);
  const rows = monitorsState.data?.monitors ?? null;
  const error = monitorsState.error;
  const query = route.query.get("q") ?? "";
  const status = monitorStatusFilter(route.query.get("status"));
  const scheduleFilter = monitorScheduleFilter(route.query.get("schedule"));
  const filteredRows = useMemo(() => filterMonitors(rows ?? [], query, status, scheduleFilter), [rows, query, status, scheduleFilter]);
  const setFilter = (key: string, value: string) => { const next = new URLSearchParams(route.query); value && value !== "all" ? next.set(key, value) : next.delete(key); navigate(`/monitors?${next}`); };

  const [name, setName] = useState("");
  const [rules, setRules] = useState<Rules>({});
  const [schedule, setSchedule] = useState(false);
  const [frequency, setFrequency] = useState("daily");
  const [busy, setBusy] = useState(false);
  const [createErr, setCreateErr] = useState("");

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setCreateErr("");
    try {
      await createMonitor({
        name: name.trim(),
        rules,
        schedule_enabled: schedule,
        schedule_frequency: frequency,
        schedule_timezone: "UTC",
      });
      setName("");
      setRules({});
      setSchedule(false);
      setShowCreate(false);
      setTick((n) => n + 1);
    } catch (e: unknown) {
      setCreateErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const setMonitorFrequency = async (monitor: MonitorSummary, value: ScheduleFrequency) => {
    setScheduleBusy(monitor.id);
    setScheduleErr("");
    try {
      await updateMonitor(monitor.id, value === "manual"
        ? { schedule_enabled: false }
        : { schedule_enabled: true, schedule_frequency: value, schedule_timezone: "UTC" });
      setTick((n) => n + 1);
    } catch (e: unknown) {
      setScheduleErr(e instanceof Error ? e.message : String(e));
    } finally {
      setScheduleBusy(null);
    }
  };

  const saveNextRun = async (monitor: MonitorSummary) => {
    const utc = inputValueToUtcStamp(runDraft);
    if (!utc) return;
    setScheduleBusy(monitor.id);
    setScheduleErr("");
    try {
      await updateMonitor(monitor.id, { next_run_at: utc });
      setEditRunFor(null);
      setTick((n) => n + 1);
    } catch (e: unknown) {
      setScheduleErr(e instanceof Error ? e.message : String(e));
    } finally {
      setScheduleBusy(null);
    }
  };

  return (
    <main className="page-main">
      <div className="page-head">
        <div>
          <h1>{t("nav.monitors")}</h1>
          <p className="page-sub">{t("mon.sub")}</p>
        </div>
        <Button ref={createTrigger} type="button" variant="primary" aria-expanded={showCreate} onClick={() => setShowCreate(true)}>{t("mon.newMonitor")}</Button>
      </div>

      {rows && <KpiStrip items={[{ label: t("mon.metric.monitors"), value: rows.length }, { label: t("mon.metric.enabled"), value: rows.filter((r) => Number(r.enabled)).length }, { label: t("mon.metric.matches"), value: rows.reduce((n, r) => n + r.current_trials, 0) }, { label: t("mon.metric.changed"), value: rows.reduce((n, r) => n + r.changed_count, 0) }]} />}
      <div className="operations-toolbar monitor-filters"><label className="compact-search"><span className="sr-only">{t("common.search")}</span><input value={query} placeholder={t("mon.searchPlaceholder")} onChange={(e) => setFilter("q", e.target.value)} /></label><select aria-label={t("mon.filterStatus")} value={status} onChange={(e) => setFilter("status", e.target.value)}><option value="all">{t("chip.all")}</option><option value="enabled">{t("mon.metric.enabled")}</option><option value="paused">{t("mon.paused")}</option></select><select aria-label={t("mon.filterSchedule")} value={scheduleFilter} onChange={(e) => setFilter("schedule", e.target.value)}><option value="all">{t("chip.all")}</option><option value="scheduled">{t("mon.scheduled")}</option><option value="manual">{t("mon.manual")}</option></select></div>
      <Drawer open={showCreate} label={t("mon.createTitle")} onClose={() => setShowCreate(false)} triggerRef={createTrigger} mobile="full"><section className="monitor-editor">
        <header className="drawer-head"><div><p className="eyebrow">{t("mon.identity")}</p><h2 className="section-title">{t("mon.createTitle")}</h2></div><Button variant="quiet" onClick={() => setShowCreate(false)}>{t("common.cancel")}</Button></header>
        <div className="form-grid">
          <label className="form-row">
            <span>{t("mon.name")}</span>
            <input className="search" value={name} placeholder={t("mon.namePlaceholder")}
                   onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="form-row-grid">
            {RULE_FIELDS.map((k) => (
              <label key={k}>
                <span>{t(`search.rule.${k}` as Parameters<Translate>[0])}</span>
                <input value={Array.isArray(rules[k]) ? rules[k].join(", ") : rules[k] || ""} onChange={(e) => setRules({ ...rules, [k]: e.target.value })} />
              </label>
            ))}
          </div>
          <small className="form-hint">{t("mon.rulesHint")}</small>
          <InlineAlert tone="info" title={t("mon.schedule")}>{t("mon.nextRunTzHint")}</InlineAlert>
          <div className="form-inline">
            <label className="check-row">
              <input type="checkbox" checked={schedule} onChange={(e) => setSchedule(e.target.checked)} />
              <span>{t("mon.enableSchedule")}</span>
            </label>
            <label className="check-row">
              <span>{t("mon.frequency")}</span>
              <select disabled={!schedule} value={frequency} onChange={(e) => setFrequency(e.target.value)}>
                <option value="hourly">{t("mon.hourly")}</option>
                <option value="daily">{t("mon.daily")}</option>
                <option value="weekly">{t("mon.weekly")}</option>
              </select>
              <small className="form-hint">UTC</small>
            </label>
          </div>
          {createErr && <div className="error-note" role="alert">{createErr}</div>}
          <div>
            <Button type="button" variant="primary" loading={busy} disabled={!name.trim()} onClick={create}>
              {t("mon.create")}
            </Button>
          </div>
        </div>
      </section></Drawer>

      {error && <EmptyState title={t("mon.loadFailed")} hint={error} />}
      {scheduleErr && <div className="error-note" role="alert">{scheduleErr}</div>}
      {!rows && !error && <Loading label={t("loading.monitors")} />}
      {rows && rows.length === 0 && <EmptyState title={t("mon.empty")} hint={t("mon.emptyHint")} />}
      {rows && rows.length > 0 && filteredRows.length === 0 && <EmptyState title={t("mon.filteredEmpty")}><Button onClick={() => navigate("/monitors")}>{t("updates.clearFilters")}</Button></EmptyState>}
      {filteredRows.length > 0 && (
        <table className="d-table monitors-table">
          <thead>
            <tr>
              <th>{t("mon.name")}</th>
              <th>{t("mon.state")}</th>
              <th>{t("mon.schedule")}</th>
              <th>{t("mon.current")}</th>
              <th>{t("mon.new")}</th>
              <th>{t("mon.changed")}</th>
              <th>{t("mon.left")}</th>
              <th>{t("mon.lastRun")}</th>
              <th>{t("mon.nextRun")}</th>
            </tr>
          </thead>
          <tbody>
            {filteredRows.map((m) => (
              <tr key={m.id}>
                <td data-label={t("mon.name")} onClick={() => navigate(`/monitors/${m.id}`)}><a href={`/monitors/${m.id}`} className="text-btn" onClick={(e) => { e.preventDefault(); navigate(`/monitors/${m.id}`); }}>{m.name}</a></td>
                <td data-label={t("mon.state")}>
                  <MonitorStatusBadge t={t} enabled={!!Number(m.enabled)} />
                </td>
                <td data-label={t("mon.schedule")}>
                  {Number(m.schedule_enabled)
                    ? `${t(`mon.${m.schedule_frequency === "hourly" ? "hourly" : m.schedule_frequency === "weekly" ? "weekly" : "daily"}` as never)}`
                    : t("mon.manual")}
                </td>
                <td data-label={t("mon.current")}>{m.current_trials}</td>
                <td data-label={t("mon.new")}>{m.new_count}</td>
                <td data-label={t("mon.changed")}>{m.changed_count}</td>
                <td data-label={t("mon.left")}>{m.left_count}</td>
                <td data-label={t("mon.lastRun")}>{m.last_run_at ? relTime(m.last_run_at) : "—"}</td>
                <td data-label={t("mon.nextRun")}>
                  <div className="monitor-schedule-control">
                    <select
                      aria-label={`${t("mon.frequency")}: ${m.name}`}
                      disabled={scheduleBusy === m.id}
                      value={Number(m.schedule_enabled) ? (m.schedule_frequency || "daily") : "manual"}
                      onChange={(e) => void setMonitorFrequency(m, e.target.value as ScheduleFrequency)}
                    >
                      <option value="manual">{t("mon.manual")}</option>
                      <option value="hourly">{t("mon.hourly")}</option>
                      <option value="daily">{t("mon.daily")}</option>
                      <option value="weekly">{t("mon.weekly")}</option>
                    </select>
                    {Number(m.schedule_enabled) && m.next_run_at ? (
                      editRunFor === m.id ? (
                        <span className="monitor-next-run-editor">
                          <input type="datetime-local" autoFocus
                                 aria-label={`${t("mon.nextRun")}: ${m.name}`}
                                 title={t("mon.nextRunTzHint")}
                                 value={runDraft}
                                 onChange={(e) => setRunDraft(e.target.value)}
                                 onKeyDown={(e) => {
                                   if (e.key === "Enter" && runDraft) void saveNextRun(m);
                                   if (e.key === "Escape") setEditRunFor(null);
                                 }} />
                          <button type="button" className="text-btn" disabled={!runDraft || scheduleBusy === m.id}
                                  aria-label={t("mon.save")}
                                  onClick={() => void saveNextRun(m)}>✓</button>
                          <button type="button" className="text-btn"
                                  aria-label={t("mon.cancelEdit")}
                                  onClick={() => setEditRunFor(null)}>✕</button>
                        </span>
                      ) : (
                        <button type="button" className="text-btn monitor-next-run"
                                title={t("mon.editNextRun")}
                                onClick={() => { setEditRunFor(m.id); setRunDraft(utcStampToInputValue(m.next_run_at)); }}>
                          {absTime(m.next_run_at)}<span aria-hidden="true"> ✎</span>
                        </button>
                      )
                    ) : <small>—</small>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

/* ── detail ───────────────────────────────────────────────────────────── */

type DetailTab = "overview" | "trials" | "activity" | "runs";
const DETAIL_TABS: DetailTab[] = ["overview", "trials", "activity", "runs"];

function MonitorDetailPage({ id, t, lang, health }: { id: number; t: Translate; lang: Lang; health: BackendHealth }) {
  void health;
  const route = useRoute();
  const [tab, setTab] = useState<DetailTab>(() => {
    const v = route.query.get("tab");
    return DETAIL_TABS.includes(v as DetailTab) ? (v as DetailTab) : "overview";
  });
  useEffect(() => {
    const v = route.query.get("tab");
    setTab(DETAIL_TABS.includes(v as DetailTab) ? (v as DetailTab) : "overview");
  }, [route.url]);

  const [tick, setTick] = useState(0);
  const [runMsg, setRunMsg] = useState("");
  const [runBusy, setRunBusy] = useState(false);
  // cached read (remounted per monitor via key): instant on revisit, and
  // Run-now/edit bumps the tick while the sticky hook keeps the page intact
  const monitorState = useStickyJson<{ monitor: MonitorSummary }>(
    refreshableUrl(`/api/monitors/${id}`, tick), isMonitorPayload);
  const m = monitorState.data?.monitor ?? null;
  const error = monitorState.error;

  if (error) return <main className="page-main"><EmptyState title={t("mon.loadFailed")} hint={error} /></main>;
  if (!m) return <main className="page-main"><Loading label={t("loading.monitors")} /></main>;

  const selectTab = (next: DetailTab) => {
    navigate(`/monitors/${id}?tab=${next}`);
  };

  return (
    <main className="page-main">
      <nav className="crumbs" aria-label={t("a11y.breadcrumb")}>
        <a href="/monitors" onClick={(e) => { e.preventDefault(); navigate("/monitors"); }}>{t("nav.monitors")}</a>
        <span aria-hidden="true">/</span>
        <span>{m.name}</span>
      </nav>

      <header className="page-head">
        <div>
          <h1>{m.name}</h1>
          <p className="page-sub">
            <MonitorStatusBadge t={t} enabled={!!Number(m.enabled)} />{" "}
            {Number(m.schedule_enabled) ? t(`mon.${m.schedule_frequency === "hourly" ? "hourly" : m.schedule_frequency === "weekly" ? "weekly" : "daily"}` as never) : t("mon.manual")}
            {m.next_run_at ? ` · ${t("mon.nextRun")}: ${absTime(m.next_run_at)}` : ""}
          </p>
        </div>
        <div className="trial-head-actions">
          <AddToProject kind="monitors" id={m.id} />
          <Button type="button" variant="primary" loading={runBusy} onClick={() => {
            setRunBusy(true); setRunMsg("");
            runMonitor(id).then(() => { setRunMsg(t("mon.runQueued")); setTick((n) => n + 1); })
              .catch((e: unknown) => setRunMsg(e instanceof Error ? e.message : t("mon.runFail"))).finally(() => setRunBusy(false));
          }}>{t("mon.runNow")}</Button>
        </div>
      </header>
      {runMsg && <p className="form-hint">{runMsg}</p>}
      <KpiStrip items={[{ label: t("mon.current"), value: m.current_trials, onClick: () => selectTab("trials") }, { label: t("mon.new"), value: m.new_count, onClick: () => selectTab("activity") }, { label: t("mon.changed"), value: m.changed_count, onClick: () => selectTab("activity") }, { label: t("mon.left"), value: m.left_count, onClick: () => selectTab("activity") }]} />

      <div className="page-tabs" role="tablist" aria-label={t("a11y.monitorTabs")}>
        {DETAIL_TABS.map((x, index) => (
          <button type="button" key={x} role="tab" aria-selected={tab === x}
                  tabIndex={tab === x ? 0 : -1} className={`ptab ${tab === x ? "ptab-on" : ""}`} onClick={() => selectTab(x)} onKeyDown={(event) => {
                    if (["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) { event.preventDefault(); const target = event.key === "Home" ? 0 : event.key === "End" ? DETAIL_TABS.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + DETAIL_TABS.length) % DETAIL_TABS.length; selectTab(DETAIL_TABS[target]); }
                  }}>
            {t(`mon.tab.${x}` as never)}
          </button>
        ))}
      </div>

      {tab === "overview" && <MonitorOverview m={m} onSaved={() => setTick((n) => n + 1)} t={t} />}
      {tab === "trials" && <MonitorTrials key={`${id}/${tick}`} id={id} t={t} />}
      {tab === "activity" && <MonitorActivity key={`${id}/${tick}`} id={id} t={t} lang={lang} />}
      {tab === "runs" && <MonitorRuns key={`${id}/${tick}`} id={id} t={t} />}
    </main>
  );
}

function MonitorOverview({ m, onSaved, t }: { m: MonitorSummary; onSaved: () => void; t: Translate }) {
  const rules: Rules = m.rules ?? {};
  const populated = RULE_FIELDS.filter((k) => ruleToText(rules[k]).trim());
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(m.name);
  const [draft, setDraft] = useState<Rules>(rules);
  const [schedule, setSchedule] = useState(!!Number(m.schedule_enabled));
  const [frequency, setFrequency] = useState<Exclude<ScheduleFrequency, "manual">>(
    m.schedule_frequency === "hourly" || m.schedule_frequency === "weekly" ? m.schedule_frequency : "daily");
  const [busy, setBusy] = useState(false);
  const [saveErr, setSaveErr] = useState("");
  const editTrigger = useRef<HTMLButtonElement>(null);

  const resetDraft = () => {
    setName(m.name);
    setDraft(rules);
    setSchedule(!!Number(m.schedule_enabled));
    setFrequency(m.schedule_frequency === "hourly" || m.schedule_frequency === "weekly" ? m.schedule_frequency : "daily");
    setSaveErr("");
  };

  return (
    <section className="panel">
      <div className="form-inline split">
        <h2 className="section-title">{t("mon.rulesTitle")}</h2>
        <button ref={editTrigger} type="button" className="text-btn" onClick={() => { resetDraft(); setEditing(true); }}>
          {editing ? t("mon.cancelEdit") : t("mon.edit")}
        </button>
      </div>

      {!editing && (
        <div>
          <button type="button" className="chip chip-on open-as-search"
                  onClick={() => navigate(`/trials?${searchStateToQuery(monitorRuleToSearchState(rules))}`)}>
            {t("mon.openAsSearch")}
          </button>
          {populated.length === 0
            ? <EmptyState title={t("mon.noRules")} />
            : <div className="rules-grid">
                {populated.map((k) => (
                  <div className="rule-item" key={k}>
                    <span className="rule-key">{k.replace(/_/g, " ")}</span>
                    <span className="rule-val">{ruleToText(rules[k])}</span>
                  </div>
                ))}
              </div>}
        </div>
      )}

      <Drawer open={editing} label={t("mon.edit")} onClose={() => setEditing(false)} triggerRef={editTrigger} mobile="full"><section className="monitor-editor">
        <header className="drawer-head"><div><p className="eyebrow">{t("mon.identity")}</p><h2>{t("mon.edit")}</h2></div><Button variant="quiet" onClick={() => setEditing(false)}>{t("common.cancel")}</Button></header><div className="form-grid">
          <label className="form-row">
            <span>{t("mon.name")}</span>
            <input className="search" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="form-row-grid">
            {RULE_FIELDS.map((k) => (
              <label key={k}>
                <span>{t(`search.rule.${k}` as Parameters<Translate>[0])}</span>
                <input value={ruleToText(draft[k])} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
              </label>
            ))}
          </div>
          <small className="form-hint">{t("mon.rulesHint")}</small>
          <InlineAlert tone="info" title={t("mon.schedule")}>{t("mon.nextRunTzHint")}</InlineAlert>
          <div className="form-inline">
            <label className="check-row">
              <input type="checkbox" checked={schedule} onChange={(e) => setSchedule(e.target.checked)} />
              <span>{t("mon.enableSchedule")}</span>
            </label>
            <label className="check-row">
              <span>{t("mon.frequency")}</span>
              <select disabled={!schedule} value={frequency} onChange={(e) => setFrequency(e.target.value as Exclude<ScheduleFrequency, "manual">)}>
                <option value="hourly">{t("mon.hourly")}</option>
                <option value="daily">{t("mon.daily")}</option>
                <option value="weekly">{t("mon.weekly")}</option>
              </select>
              <small className="form-hint">UTC</small>
            </label>
          </div>
          {saveErr && <div className="error-note" role="alert">{saveErr}</div>}
          <div>
            <Button type="button" variant="primary" loading={busy} onClick={() => {
              setBusy(true);
              setSaveErr("");
              updateMonitor(m.id, {
                name: name.trim() || m.name,
                rules: draftToRule(draft),
                schedule_enabled: schedule,
                schedule_frequency: frequency,
                schedule_timezone: "UTC",
              }).then(() => {
                setEditing(false);
                onSaved();
              }).catch((e: unknown) => {
                setSaveErr(e instanceof Error ? e.message : String(e));
              }).finally(() => setBusy(false));
            }}>{t("mon.save")}</Button>
          </div>
        </div>
      </section></Drawer>
    </section>
  );
}

function MonitorTrials({ id, t }: { id: number; t: Translate }) {
  const state = useStickyJson<{ trials: MonitorTrialRow[] }>(
    `/api/monitors/${id}/trials`, isMonitorTrialsPayload);
  const rows = state.data?.trials ?? null;
  const error = state.error;
  if (error) return <EmptyState title={t("mon.trialsLoadFailed")} hint={error} />;
  if (!rows) return <Loading label={t("loading.monitors")} />;
  if (rows.length === 0) return <EmptyState title={t("mon.noTrials")} hint={t("mon.noTrialsHint")} />;
  return (
    <table className="d-table">
      <thead>
        <tr>
          <th>{t("sources.id")}</th><th>{t("trials.colTitle")}</th><th>{t("status.all")}</th>
          <th>{t("sources.registry")}</th><th>{t("mon.matchState")}</th><th>{t("mon.lastMatched")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="tid"><TrialLink source={r.source || "NCT"} id={r.trial_id} /></td>
            <td className="mon-trial-title"><TrialLink source={r.source || "NCT"} id={r.trial_id} title={r.title || r.trial_id} /></td>
            <td><span className={`pill ${statusTone(r.status || "")}`}>{r.status || "—"}</span></td>
            <td>
              <span className="src-badge src-badge-sm"
                    style={{ backgroundColor: SOURCE_BADGES[r.source || ""]?.color ?? "#64748b" }}>
                {SOURCE_BADGES[r.source || ""]?.label ?? r.source}
              </span>
            </td>
            <td>{Number(r.currently_matches) ? t("mon.matching") : t("mon.leftMonitor")}</td>
            <td>{r.last_matched_at ? relTime(r.last_matched_at) : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MonitorActivity({ id, t, lang }: { id: number; t: Translate; lang: Lang }) {
  const state = useStickyJson<{ activity: MonitorActivityRow[] }>(
    `/api/monitors/${id}/activity`, isMonitorActivityPayload);
  const rows = state.data?.activity ?? null;
  const error = state.error;
  if (error) return <EmptyState title={t("mon.activityLoadFailed")} hint={error} />;
  if (!rows) return <Loading label={t("loading.monitors")} />;
  if (rows.length === 0) return <EmptyState title={t("mon.noActivity")} hint={t("mon.noActivityHint")} />;

  const typeLabel = (ev: MonitorActivityRow): string => {
    if (ev.event_type === "trial_changed") return t("mon.evChanged");
    if (ev.event_type === "trial_entered") return t("mon.evEntered");
    if (ev.event_type === "trial_left") return t("mon.evLeft");
    if (ev.event_type === "monitor_created") return t("mon.evCreated");
    return ev.event_type;
  };

  return (
    <div className="change-list">
      {rows.map((ev) => (
        <article className="change-row" key={ev.id}>
          <span className={`pill ${ev.event_type === "trial_changed" ? "tone-blue" : ev.event_type === "trial_left" ? "tone-gray" : "tone-green"}`}>
            {typeLabel(ev)}
          </span>
          {ev.severity && ev.event_type === "trial_changed" && <SeverityBadge severity={ev.severity} t={t} />}
          <div className="change-main">
            <TrialLink source={ev.source || "NCT"} id={ev.trial_id} className="update-title" />
            {ev.field_name && (
              <div className="update-diff">
                <span className="update-field">{fieldLabel(ev.field_name, lang, { field_label: ev.field_label, field_label_zh: ev.field_label_zh })}</span>
                <ChangeValue value={ev.old_value} />
                <span className="diff-arrow">→</span>
                <ChangeValue value={ev.new_value} />
              </div>
            )}
            <small className="change-meta"><span className="tid">{ev.trial_id}</span> · {relTime(ev.detected_at)}</small>
          </div>
          <a className="text-btn"
             href={`/trials/${encodeURIComponent(ev.source || "NCT")}/${encodeURIComponent(ev.trial_id)}?tab=changes${ev.trial_event_id ? `&event=${ev.trial_event_id}` : ""}`}
             onClick={(e) => {
               if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
               e.preventDefault();
               navigate(`/trials/${encodeURIComponent(ev.source || "NCT")}/${encodeURIComponent(ev.trial_id)}?tab=changes${ev.trial_event_id ? `&event=${ev.trial_event_id}` : ""}`);
             }}>
            {t("mon.openChanges")}
          </a>
        </article>
      ))}
    </div>
  );
}

function MonitorRuns({ id, t }: { id: number; t: Translate }) {
  const state = useStickyJson<{ runs: MonitorRunRow[] }>(
    `/api/monitors/${id}/runs`, isMonitorRunsPayload);
  const rows = state.data?.runs ?? null;
  const error = state.error;
  if (error) return <EmptyState title={t("mon.runsLoadFailed")} hint={error} />;
  if (!rows) return <Loading label={t("loading.monitors")} />;
  if (rows.length === 0) return <EmptyState title={t("mon.noRuns")} hint={t("mon.noRunsHint")} />;
  return (
    <table className="d-table">
      <thead>
        <tr>
          <th>{t("mon.started")}</th><th>{t("mon.status")}</th><th>{t("mon.trigger")}</th>
          <th>{t("mon.matched")}</th><th>{t("mon.new")}</th><th>{t("mon.changed")}</th><th>{t("mon.left")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{absTime(r.started_at)}</td>
            <td>
              <span className={`pill ${r.status === "completed" ? "tone-green" : r.status === "failed" ? "tone-red" : "tone-blue"}`}>
                {r.status}
              </span>
              {r.error_message && <div className="change-meta">{r.error_message}</div>}
            </td>
            <td>{r.trigger || "manual"}</td>
            <td>{r.matched_count}</td>
            <td>{r.new_count}</td>
            <td>{r.changed_count}</td>
            <td>{r.left_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
