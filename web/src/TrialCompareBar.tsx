import type { Translate } from "./i18n";
import type { Trial } from "./types";
import { trialSelectionKey } from "./trialViewState";

export default function TrialCompareBar({ trials, currentKeys, t, onRemove, onClear, onCompare }: {
  trials: Trial[]; currentKeys: Set<string>; t: Translate;
  onRemove: (trial: Trial) => void; onClear: () => void; onCompare: () => void;
}) {
  if (trials.length < 2) return null;
  const hidden = trials.filter((trial) => !currentKeys.has(trialSelectionKey(trial))).length;
  return <aside className="trial-compare-bar" aria-label={t("compare.barLabel")}>
    <div className="trial-compare-summary" aria-live="polite">
      <strong>{t("search.selected", { n: trials.length })}</strong>
      {hidden > 0 && <span>{t("compare.outsideResults", { n: hidden })}</span>}
    </div>
    <div className="trial-compare-items">
      {trials.map((trial) => <span key={trialSelectionKey(trial)}>
        <b>{trial.source}</b> {trial.title}
        <button type="button" aria-label={t("compare.remove", { title: trial.title })} onClick={() => onRemove(trial)}>×</button>
      </span>)}
    </div>
    <div className="trial-compare-actions">
      <button type="button" className="ui-button ui-button-quiet" onClick={onClear}>{t("search.clearSelection")}</button>
      <button type="button" className="ui-button ui-button-primary" onClick={onCompare}>{t("search.compareSelected")}</button>
    </div>
  </aside>;
}
