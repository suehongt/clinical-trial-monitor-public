import AddToProject from "./AddToProject";
import type { Translate } from "./i18n";
import { SOURCE_BADGES, phaseLabel, statusTone } from "./labels";
import { navigate } from "./router";
import type { Severity, Trial } from "./types";
import { RegistryBadge, SeverityBadge, StatusBadge, relTime } from "./ui";
import { trialSelectionKey, type TrialDensity } from "./trialViewState";

export interface TrialRowWatchMeta {
  unseen?: number;
  severity?: Severity | null;
  lastChanged?: string | null;
}

export default function TrialRow({ trial, t, density = "compact", selected = false,
  watched = false, recent, watchMeta, onSelect, onWatch, onPreview, watchBusy = false }: {
  trial: Trial; t: Translate; density?: TrialDensity; selected?: boolean; watched?: boolean;
  recent?: "new" | "changed"; watchMeta?: TrialRowWatchMeta;
  onSelect?: (trial: Trial) => void; onWatch?: (trial: Trial) => void;
  onPreview?: (trial: Trial, trigger: HTMLButtonElement) => void; watchBusy?: boolean;
}) {
  const key = trialSelectionKey(trial);
  const detailUrl = `/trials/${encodeURIComponent(trial.source)}/${encodeURIComponent(trial.id)}`;
  const conditions = trial.conditions.filter(Boolean);
  const interventions = trial.interventions.filter(Boolean);
  const context = conditions.length ? conditions : interventions;
  const contextLabel = conditions.length ? t("field.conditions") : t("field.intervention");
  const open = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault(); navigate(detailUrl);
  };
  return <article className={`trial-row trial-row-${density} ${selected ? "is-selected" : ""}`}
                  role="listitem" aria-labelledby={`trial-title-${cssId(key)}`}>
    <div className="trial-row-select">
      {onSelect && <input type="checkbox" checked={selected}
        aria-label={t("search.selectTrial", { title: trial.title })}
        onChange={() => onSelect(trial)} />}
    </div>
    <div className="trial-row-body">
      <div className="trial-row-kicker">
        <RegistryBadge fullName={SOURCE_BADGES[trial.source]?.label}>{SOURCE_BADGES[trial.source]?.label ?? trial.source}</RegistryBadge>
        <StatusBadge tone={statusToneName(trial.status)}>{trial.status || t("common.notProvided")}</StatusBadge>
        {recent && <span className={`trial-change-flag trial-change-${recent}`}>{recent === "new" ? t("badge.new") : t("badge.changed")}</span>}
        {watchMeta?.severity && <SeverityBadge severity={watchMeta.severity} t={t} />}
      </div>
      <h2 className="trial-row-title" id={`trial-title-${cssId(key)}`} title={trial.title}>
        <a href={detailUrl} onClick={open}>{trial.title}</a>
      </h2>
      <div className="trial-row-meta">
        <span>{phaseLabel(trial.phase, t)}</span>
        <span>{trial.studyType || t("common.notProvided")}</span>
        <span className="trial-row-enrollment"><b>{t("field.enrollment")}</b> {trial.enrollment == null ? t("common.notProvided") : trial.enrollment.toLocaleString()}</span>
        <span><b>{t("field.lastUpdated")}</b> {trial.lastUpdated ? relTime(trial.lastUpdated) : t("common.notProvided")}</span>
      </div>
      {context.length > 0 && <p className="trial-row-context" title={context.join("; ")}>
        <b>{contextLabel}</b> {context.slice(0, 2).join(" · ")}{context.length > 2 && <span> +{context.length - 2}</span>}
      </p>}
      {watchMeta && <p className="trial-row-watch-meta">
        {watchMeta.unseen ? t("watched.unseen", { n: watchMeta.unseen }) : t("watched.noNew")}
        {watchMeta.lastChanged && <> · {t("watched.lastChanged")}: {relTime(watchMeta.lastChanged)}</>}
      </p>}
    </div>
    <div className="trial-row-facts">
      <span className="trial-row-id">{trial.id}</span>
    </div>
    <div className="trial-row-actions" aria-label={t("trials.rowActions", { title: trial.title })}>
      {onWatch && <button type="button" className="ui-button ui-button-quiet" disabled={watchBusy}
        aria-pressed={watched} onClick={() => onWatch(trial)}>
        {watched ? t("trial.watching") : t("trial.watch")}
      </button>}
      <AddToProject kind="trials" source={trial.source} trialId={trial.id} />
      {onPreview && <button type="button" className="ui-button ui-button-secondary"
        onClick={(event) => onPreview(trial, event.currentTarget)}>{t("trials.quickPreview")}</button>}
      <a className="ui-button ui-button-quiet trial-row-open" href={detailUrl} onClick={open}>{t("card.details")}</a>
    </div>
  </article>;
}

function cssId(value: string): string { return value.replace(/[^a-zA-Z0-9_-]/g, "-"); }

function statusToneName(status: string): "positive" | "warning" | "critical" | "normal" | "unknown" {
  const tone = statusTone(status);
  if (tone.includes("recruiting") || tone.includes("good")) return "positive";
  if (tone.includes("terminated") || tone.includes("bad")) return "critical";
  if (tone.includes("pending") || tone.includes("warn")) return "warning";
  return status ? "normal" : "unknown";
}
