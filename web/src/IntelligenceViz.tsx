import { useId, useState, type ReactNode } from "react";
import { SOURCE_BADGES } from "./labels";
import type { Lang, Translate } from "./i18n";
import type { ScopedUpdateItem } from "./types";
import { fieldLabel } from "./fieldLabels";
import { navigate } from "./router";
import { formatChangeValue, sortCategories, summarizeFreshness } from "./intelligenceState";
import { SeverityBadge, absTime } from "./ui";

export type TrendBucket = { date: string; critical: number; important: number; normal: number; minor?: number; entered?: number; left?: number; reentered?: number };
export type IntelligenceMetricItem = { label: string; value?: number | string; note?: string; severity?: string; primary?: boolean; unavailable?: boolean; onClick?: () => void };

export function PageSection({ title, action, children, className = "" }: { title: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`analysis-panel ${className}`}><header className="analysis-panel-head"><h2>{title}</h2>{action}</header>{children}</section>;
}

export function IntelligenceMetric({ item }: { item: IntelligenceMetricItem }) {
  const body = <><span className="kpi-label">{item.label}</span><strong>{item.unavailable ? "—" : typeof item.value === "number" ? item.value.toLocaleString() : item.value ?? "—"}</strong>{item.note && <span className="kpi-note">{item.note}</span>}</>;
  return item.onClick ? <button type="button" className={`kpi ${item.primary ? "kpi-primary" : ""} ${item.severity ? `kpi-${item.severity}` : ""}`} onClick={item.onClick}>{body}</button>
    : <div className={`kpi ${item.primary ? "kpi-primary" : ""} ${item.severity ? `kpi-${item.severity}` : ""}`}>{body}</div>;
}

export function IntelligenceMetricStrip({ items }: { items: IntelligenceMetricItem[] }) { return <div className="kpi-strip">{items.slice(0, 4).map((item) => <IntelligenceMetric item={item} key={item.label} />)}</div>; }
export const KpiStrip = IntelligenceMetricStrip;

export function ActivityTrend({ buckets, empty, t, showMembership = false }: { buckets: TrendBucket[]; empty: string; t: Translate; showMembership?: boolean }) {
  const max = Math.max(1, ...buckets.map((b) => b.critical + b.important + b.normal + (b.minor ?? 0)));
  const hasActivity = buckets.some((b) => b.critical + b.important + b.normal + (b.minor ?? 0) + (b.entered ?? 0) + (b.left ?? 0) + (b.reentered ?? 0) > 0);
  if (!buckets.length || !hasActivity) return <p className="form-hint">{empty}</p>;
  const describe = (b: TrendBucket) => `${b.date}: ${b.critical} ${t("sev.critical")}, ${b.important} ${t("sev.important")}, ${b.normal} ${t("sev.normal")}, ${b.minor ?? 0} ${t("sev.minor")}${showMembership ? `, ${b.entered ?? 0} ${t("intel.entered")}, ${b.left ?? 0} ${t("intel.left")}, ${b.reentered ?? 0} ${t("intel.reentered")}` : ""}`;
  return <figure className={`trend-wrap ${showMembership ? "trend-wrap-membership" : ""}`}><figcaption className="sr-only">{buckets.map(describe).join("; ")}</figcaption><div className="trend-chart" aria-label={t("intel.trendAlt")}>
    {buckets.map((b) => <button type="button" className="trend-day" key={b.date} aria-label={describe(b)} title={describe(b)}><span className="trend-stack" style={{ height: `${Math.max(2, (b.critical + b.important + b.normal + (b.minor ?? 0)) / max * 100)}%` }} aria-hidden="true">{b.normal + (b.minor ?? 0) > 0 && <span className="trend-normal" style={{ flex: b.normal + (b.minor ?? 0) }} />}{b.important > 0 && <span className="trend-important" style={{ flex: b.important }} />}{b.critical > 0 && <span className="trend-critical" style={{ flex: b.critical }} />}</span><small>{b.date.slice(5)}</small>{showMembership && <small className="trend-membership" aria-hidden="true">+{b.entered ?? 0} / −{b.left ?? 0}</small>}</button>)}
  </div><div className="trend-legend"><span className="trend-key trend-critical">{t("sev.critical")}</span><span className="trend-key trend-important">{t("sev.important")}</span><span className="trend-key trend-normal">{t("sev.normal")}</span></div></figure>;
}

export function categoryLabel(name: string, t: Translate): string { const key = `intel.category.${name}`; return ["recruitment", "enrollment", "outcomes", "timeline", "sponsor", "geography", "intervention", "eligibility", "other", "design"].includes(name) ? t(key as Parameters<Translate>[0]) : name.replace(/_/g, " "); }

export function CategoryBreakdown({ categories, empty, t, onSelect }: { categories: Record<string, number>; empty: string; t: Translate; onSelect?: (category: string) => void }) {
  const rows = sortCategories(categories); const total = rows.reduce((sum, [, n]) => sum + n, 0); const max = Math.max(1, ...rows.map(([, n]) => n));
  if (!rows.length) return <p className="form-hint">{empty}</p>;
  return <div className="category-bars">{rows.map(([name, count]) => { const content = <><span title={categoryLabel(name, t)}>{categoryLabel(name, t)}</span><span className="category-track" aria-hidden="true"><i style={{ width: `${count / max * 100}%` }} /></span><strong>{count} <small>· {Math.round(count / Math.max(1, total) * 100)}%</small></strong></>; return onSelect ? <button type="button" className="category-row" key={name} onClick={() => onSelect(name)}>{content}</button> : <div className="category-row" key={name}>{content}</div>; })}</div>;
}
export const CategoryBars = CategoryBreakdown;

export function FreshnessBadge({ freshness, t }: { freshness: string; t: Translate }) { const key = `ws.fresh.${freshness}`; const label = ["fresh", "delayed", "stale", "unknown", "disabled"].includes(freshness) ? t(key as Parameters<Translate>[0]) : freshness; return <span className={`status-badge freshness-${freshness}`}><span aria-hidden="true" className="freshness-dot" />{label}</span>; }

export type FreshnessSource = { full_name?: string; name?: string; freshness?: string; state?: string; last_successful_sync?: string | null; last_success?: string | null; stale_reason?: string };
export function FreshnessSummary({ sources, t }: { sources: FreshnessSource[]; t: Translate }) {
  const [open, setOpen] = useState(false); const id = useId(); const summary = summarizeFreshness(sources);
  return <section className="freshness-summary"><button type="button" className="freshness-summary-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}><span>{t("ws.sourceFreshness")}</span><span>{Object.entries(summary).map(([state, count]) => <span key={state}><FreshnessBadge freshness={state} t={t} /> {count}</span>)}</span></button>{open && <div id={id} className="freshness-detail"><ul>{sources.map((source, index) => { const state = source.freshness ?? source.state ?? "unknown"; return <li key={`${source.full_name ?? source.name}:${index}`}><strong>{source.full_name ?? source.name ?? t("ws.fresh.unknown")}</strong><FreshnessBadge freshness={state} t={t} /><small>{absTime(source.last_successful_sync ?? source.last_success)}{source.stale_reason ? ` · ${source.stale_reason}` : ""}</small></li>; })}</ul><button type="button" className="text-btn" onClick={() => navigate("/data-sources")}>{t("ws.viewSources")}</button></div>}</section>;
}

export function RegistryBadge({ registry }: { registry: string }) { return <span className="status-badge registry-badge">{SOURCE_BADGES[registry]?.label ?? registry}</span>; }
export function MonitorStatusBadge({ enabled, t }: { enabled: boolean; t: Translate }) { return <span className={`status-badge ${enabled ? "freshness-fresh" : "freshness-unknown"}`}>{t(enabled ? "mon.active" : "mon.paused")}</span>; }

export function ChangeRow({ item, t, lang, addon }: { item: ScopedUpdateItem; t: Translate; lang: Lang; addon?: ReactNode }) {
  const [open, setOpen] = useState(false); const detailId = useId(); const oldValue = formatChangeValue(item.old_value, t("intel.notReported")); const newValue = formatChangeValue(item.new_value, t("intel.notReported")); const long = oldValue.length + newValue.length > 180;
  const href = `/trials/${encodeURIComponent(item.source)}/${encodeURIComponent(item.trial_id)}?tab=changes&event=${item.event_id}`;
  return <article className={`intel-change-row severity-edge-${item.severity}`}><div className="intel-change-severity"><SeverityBadge severity={item.severity} t={t} />{item.watched && <span className="watched-label">{t("ws.watched")}</span>}</div><div className="intel-change-main"><a className="intel-change-title" href={href} onClick={(event) => { event.preventDefault(); navigate(href); }}>{item.title}</a><div className="intel-change-provenance"><RegistryBadge registry={item.source} /><span className="tid">{item.trial_id}</span><span>{fieldLabel(item.field_name, lang, item)}</span><time dateTime={item.detected_at}>{absTime(item.detected_at)}</time></div><div className={`intel-change-diff ${!open && long ? "is-clamped" : ""}`} id={detailId}><span><b>{t("intel.oldValue")}</b>{oldValue}</span><span aria-hidden="true">→</span><span><b>{t("intel.newValue")}</b>{newValue}</span></div>{(item.monitors.length > 0 || (item.project_monitors?.length ?? 0) > 0) && <small className="intel-change-context">{[...item.monitors, ...(item.project_monitors ?? [])].join(" · ")}</small>}{long && <button type="button" className="text-btn change-expand" aria-expanded={open} aria-controls={detailId} onClick={() => setOpen(!open)}>{open ? t("intel.showLess") : t("intel.showMore")}</button>}</div><div className="intel-change-actions">{addon}<a role="button" aria-label={`${t(`sev.${item.severity}` as Parameters<Translate>[0])} ${item.title}`} className="text-btn" href={href} onClick={(event) => { event.preventDefault(); navigate(href); }}>{t("intel.openChange")}</a></div></article>;
}
