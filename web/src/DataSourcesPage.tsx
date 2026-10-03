import { useMemo, useState } from "react";
import { refreshableUrl, useJson, useStickyJson } from "./api";
import { Button, EmptyState, InlineAlert, Loading, StatusBadge, absTime, relTime } from "./ui";
import { navigate, useRoute } from "./router";
import type { BackendHealth } from "./health";
import type { Translate } from "./i18n";
import { FreshnessBadge, IntelligenceMetricStrip, RegistryBadge } from "./IntelligenceViz";
import { freshnessSummary, retryEligible } from "./phase4State";

interface RegistryStatus { registry: string; short_name: string; full_name: string; enabled: boolean; freshness: string; last_attempted_sync?: string | null; last_successful_sync?: string | null; source_current_through?: string | null; next_sync_at?: string | null; latest_run_status?: string | null; last_run_id?: number | null; consecutive_failures?: number | null; }
interface Run { run_id: number; status: string; trigger: string; started_at: string; completed_at?: string | null; records_fetched?: number | null; records_new?: number | null; records_updated?: number | null; records_unchanged?: number | null; records_failed?: number | null; trial_events_created?: number | null; failure_stage?: string | null; error_message?: string | null; }
type RunDetail = { run: Run & { short_name?: string; full_name?: string }; failures: Array<{ source_trial_id?: string; stage?: string; error_message?: string }> };
const isPayload = (v: unknown): v is { registries: RegistryStatus[] } => typeof v === "object" && v !== null && Array.isArray((v as { registries?: unknown }).registries);
const isRunDetail = (v: unknown): v is RunDetail => typeof v === "object" && v !== null && typeof (v as RunDetail).run === "object" && Array.isArray((v as RunDetail).failures);
const tone = (status: string): "positive" | "warning" | "critical" | "normal" | "unknown" => status === "succeeded" || status === "completed" ? "positive" : status === "failed" ? "critical" : status === "interrupted" ? "warning" : status === "running" ? "normal" : "unknown";

async function post(url: string) {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) { const payload = await response.json().catch(() => null) as { detail?: string } | null; throw new Error(payload?.detail || `Request failed (${response.status})`); }
  return response.json().catch(() => ({}));
}

export default function DataSourcesPage({ health, t }: { health: BackendHealth; t: Translate }) {
  const route = useRoute();
  const rawRun = route.query.get("run");
  const selectedRun = rawRun && /^\d+$/.test(rawRun) ? Number(rawRun) : null;
  const [busy, setBusy] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const statusState = useStickyJson<{ registries: RegistryStatus[] }>(health.state === "online" ? refreshableUrl("/api/registries/status", tick) : null, isPayload);
  const runState = useJson<RunDetail>(selectedRun ? refreshableUrl(`/api/registry-runs/${selectedRun}`, tick) : null, isRunDetail);
  const rows = statusState.data?.registries ?? null;
  const summary = useMemo(() => freshnessSummary(rows ?? []), [rows]);
  const mutate = async (key: string, url: string) => {
    setBusy(key); setActionErrors((old) => ({ ...old, [key]: "" }));
    try { await post(url); setTick((value) => value + 1); }
    catch (error) { setActionErrors((old) => ({ ...old, [key]: error instanceof Error ? error.message : String(error) })); }
    finally { setBusy(null); }
  };
  if (health.state !== "online") return <section className="page-main"><h1>{t("nav.dataSources")}</h1><InlineAlert tone={health.state === "offline" ? "warning" : "error"} title={t(health.state === "offline" ? "ds.offline" : "ds.backendUnavailable")} /></section>;
  return <section className="page-main operations-page">
    <div className="page-head"><div><h1>{t("nav.dataSources")}</h1><p className="page-sub">{t("ds.subtitle")}</p><small className="form-hint">Clinical Trial Monitor {health.appVersion ?? "—"} · Schema v{health.schemaVersion ?? "—"}</small></div></div>
    {statusState.error && <InlineAlert tone="error" title={t("ds.unavailable")}><p>{statusState.error}</p><Button onClick={() => setTick((v) => v + 1)}>{t("common.retry")}</Button></InlineAlert>}
    {!rows && !statusState.error && <Loading label={t("ds.loading")} />}
    {rows && <>
      <IntelligenceMetricStrip items={[{ label: t("ds.metric.enabled"), value: summary.enabled }, { label: t("fresh.fresh"), value: summary.fresh }, { label: t("ds.metric.attention"), value: summary.attention }, { label: t("ds.metric.failed"), value: summary.failed, severity: summary.failed ? "critical" : undefined }]} />
      {rows.length === 0 && <EmptyState title={t("ds.empty")} />}
      <div className="operations-list source-list" role="list">
        {rows.map((row) => <article className={`operations-row source-row ${["stale", "delayed"].includes(row.freshness) || retryEligible(row.latest_run_status ?? "") ? "needs-attention" : ""}`} role="listitem" key={row.registry}>
          <div className="operations-row-main"><div className="operations-row-title"><RegistryBadge registry={row.registry} /><h2>{row.full_name}</h2></div><div className="status-cluster"><FreshnessBadge t={t} freshness={row.freshness} /><StatusBadge tone={row.enabled ? "positive" : "unknown"}>{row.enabled ? t("mon.active") : t("fresh.disabled")}</StatusBadge>{row.latest_run_status && <StatusBadge tone={tone(row.latest_run_status)}>{row.latest_run_status}</StatusBadge>}</div></div>
          <dl className="operations-facts"><div><dt>{t("ds.col.lastAttempt")}</dt><dd>{row.last_attempted_sync ? relTime(row.last_attempted_sync) : "—"}</dd></div><div><dt>{t("ds.col.lastSync")}</dt><dd>{row.last_successful_sync ? absTime(row.last_successful_sync) : t("ds.neverSynced")}</dd></div><div><dt>{t("ds.col.currentThrough")}</dt><dd>{row.source_current_through ?? "—"}</dd></div><div><dt>{t("ds.col.nextSync")}</dt><dd>{row.next_sync_at ? absTime(row.next_sync_at) : "—"}</dd></div><div><dt>{t("ds.col.failures")}</dt><dd>{row.consecutive_failures ?? 0}</dd></div></dl>
          {actionErrors[row.registry] && <div className="error-note" role="alert">{actionErrors[row.registry]}</div>}
          <div className="operations-row-actions"><Button variant="primary" loading={busy === row.registry} disabled={!row.enabled || row.freshness === "disabled"} onClick={() => mutate(row.registry, `/api/registries/${encodeURIComponent(row.registry)}/sync`)}>{t("ds.syncNow")}</Button>{row.last_run_id && <Button variant="quiet" onClick={() => navigate(`/data-sources?run=${row.last_run_id}`)}>{t("ds.runHistory")}</Button>}</div>
        </article>)}
      </div><p className="form-hint">{t("ds.timestampsNote")}</p>
    </>}
    {rawRun && selectedRun === null && <InlineAlert tone="warning" title={t("ds.invalidRun")} action={<Button onClick={() => navigate("/data-sources")}>{t("ds.closeRun")}</Button>} />}
    {selectedRun && <section className="run-detail-panel" aria-label={t("ds.runHistory")}>
      <div className="section-head"><div><p className="eyebrow">{t("ds.runHistory")}</p><h2>{runState.status === "ready" ? `${runState.data.run.full_name ?? runState.data.run.short_name ?? ""} · #${selectedRun}` : `#${selectedRun}`}</h2></div><Button variant="quiet" onClick={() => navigate("/data-sources")}>{t("ds.closeRun")}</Button></div>
      {runState.status === "loading" && <Loading label={t("ds.loadingRuns")} />}
      {runState.status === "error" && <InlineAlert tone="error" title={t("ds.invalidRun")}><span>{runState.message}</span></InlineAlert>}
      {runState.status === "ready" && <RunSummary run={runState.data.run} t={t} busy={busy === `retry-${selectedRun}`} error={actionErrors[`retry-${selectedRun}`]} onRetry={() => mutate(`retry-${selectedRun}`, `/api/registry-runs/${selectedRun}/retry`)} />}
    </section>}
  </section>;
}

function RunSummary({ run, t, busy, error, onRetry }: { run: Run; t: Translate; busy: boolean; error?: string; onRetry: () => void }) {
  const fields: Array<[string, string | number | null | undefined]> = [[t("ds.col.started"), absTime(run.started_at)], [t("ds.completed"), run.completed_at ? absTime(run.completed_at) : "—"], [t("mon.trigger"), run.trigger], [t("ds.col.fetched"), run.records_fetched], [t("ds.col.new"), run.records_new], [t("ds.col.updated"), run.records_updated], [t("ds.col.unchanged"), run.records_unchanged], [t("ds.col.failed"), run.records_failed], [t("ds.col.events"), run.trial_events_created]];
  return <div className="run-summary"><div className="run-summary-status"><StatusBadge tone={tone(run.status)}>{run.status}</StatusBadge>{retryEligible(run.status) && <Button variant="secondary" loading={busy} onClick={onRetry}>{t("ds.retry")}</Button>}</div><dl className="operations-facts run-facts">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "—"}</dd></div>)}</dl>{run.failure_stage && <p><strong>{t("ds.failureStage")}:</strong> {run.failure_stage}</p>}{run.error_message && <details><summary>{t("ds.errorDetails")}</summary><pre className="run-error">{run.error_message}</pre></details>}{error && <div className="error-note" role="alert">{error}</div>}</div>;
}
