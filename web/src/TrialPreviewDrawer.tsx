import { useEffect, useId, useState } from "react";
import AddToProject from "./AddToProject";
import type { Translate } from "./i18n";
import { SOURCE_BADGES, phaseLabel } from "./labels";
import { navigate } from "./router";
import { loadTrialDetail, peekTrialDetail } from "./trialDetailCache";
import type { Trial, TrialDetailFull } from "./types";
import { Button, Drawer, RegistryBadge, Skeleton, StatusBadge } from "./ui";
import { safeExternalUrl } from "./urlSafety";

export default function TrialPreviewDrawer({ trial, t, triggerRef, watched, selected,
  onClose, onWatch, onSelect }: {
  trial: Trial | null; t: Translate; triggerRef: React.RefObject<HTMLElement | null>;
  watched: boolean; selected: boolean; onClose: () => void;
  onWatch: (trial: Trial) => void; onSelect: (trial: Trial) => void;
}) {
  const titleId = useId();
  const [detail, setDetail] = useState<TrialDetailFull | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const fetchDetail = (retry = false) => {
    if (!trial) return;
    const source = trial.source, id = trial.id;
    setLoading(true); setError("");
    loadTrialDetail(source, id, retry).then((value) => {
      if (trial.source === source && trial.id === id) setDetail(value);
    }).catch((reason: unknown) => {
      if (trial.source === source && trial.id === id) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (trial.source === source && trial.id === id) setLoading(false);
    });
  };
  useEffect(() => {
    if (!trial) { setDetail(null); setError(""); setLoading(false); return; }
    setDetail(peekTrialDetail(trial.source, trial.id) ?? null);
    setError("");
    fetchDetail(false);
    // keyed only by trial identity; callbacks intentionally remain local.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trial?.source, trial?.id]);
  const resolved = detail?.trial ?? trial;
  const external = safeExternalUrl(resolved?.url);
  const openFull = () => {
    if (!trial) return;
    navigate(`/trials/${encodeURIComponent(trial.source)}/${encodeURIComponent(trial.id)}`);
  };
  return <Drawer open={!!trial} label={t("preview.title")} onClose={onClose} triggerRef={triggerRef} mobile="full">
    <section className="trial-preview" aria-labelledby={titleId} aria-busy={loading || undefined}>
      <header className="trial-preview-head">
        <div><span className="trial-preview-eyebrow">{t("preview.title")}</span><h2 id={titleId}>{trial?.title}</h2></div>
        <button type="button" className="ui-icon-button" aria-label={t("a11y.close")} onClick={onClose}>×</button>
      </header>
      {!resolved ? <Skeleton label={t("detail.loading")} lines={8} /> : <>
        <div className="trial-preview-badges">
          <RegistryBadge fullName={SOURCE_BADGES[resolved.source]?.label}>{SOURCE_BADGES[resolved.source]?.label ?? resolved.source}</RegistryBadge>
          <StatusBadge>{resolved.status || t("common.notProvided")}</StatusBadge>
          <span className="trial-row-id">{resolved.id}</span>
        </div>
        {resolved.scientificTitle && resolved.scientificTitle !== resolved.title && <p className="trial-preview-scientific">{resolved.scientificTitle}</p>}
        <dl className="trial-preview-facts">
          <Fact label={t("field.phase")} value={phaseLabel(resolved.phase, t)} />
          <Fact label={t("field.studyType")} value={resolved.studyType} missing={t("common.notProvided")} />
          <Fact label={t("field.enrollment")} value={resolved.enrollment?.toLocaleString()} missing={t("common.notProvided")} />
          <Fact label={t("field.sponsor")} value={resolved.sponsors[0]} missing={t("common.notProvided")} />
          <Fact label={t("field.lastUpdated")} value={resolved.lastUpdated} missing={t("common.notProvided")} />
        </dl>
        <PreviewList label={t("field.conditions")} values={resolved.conditions} missing={t("common.notProvided")} />
        <PreviewList label={t("field.intervention")} values={resolved.interventions} missing={t("common.notProvided")} />
        {detail?.summary && <div className="trial-preview-summary"><h3>{t("field.purpose")}</h3><p>{detail.summary}</p></div>}
      </>}
      {loading && !detail && <Skeleton label={t("detail.loading")} lines={6} />}
      {error && <div className="ui-alert ui-alert-error" role="alert"><div><strong>{t("preview.loadFailed")}</strong><p>{error}</p></div><Button onClick={() => fetchDetail(true)}>{t("service.retry")}</Button></div>}
      {trial && <footer className="trial-preview-actions">
        <Button variant={watched ? "primary" : "secondary"} aria-pressed={watched} onClick={() => onWatch(trial)}>{watched ? t("trial.watching") : t("trial.watch")}</Button>
        <AddToProject kind="trials" source={trial.source} trialId={trial.id} />
        <Button variant="secondary" aria-pressed={selected} onClick={() => onSelect(trial)}>{selected ? t("preview.removeCompare") : t("preview.addCompare")}</Button>
        <Button variant="primary" onClick={openFull}>{t("preview.openFull")}</Button>
        {external && <a className="ui-button ui-button-quiet" href={external} target="_blank" rel="noreferrer">{t("link.registry")} ↗</a>}
      </footer>}
    </section>
  </Drawer>;
}

function Fact({ label, value, missing = "" }: { label: string; value: string | null | undefined; missing?: string }) {
  return <div><dt>{label}</dt><dd>{value || missing}</dd></div>;
}
function PreviewList({ label, values, missing }: { label: string; values: string[]; missing: string }) {
  return <div className="trial-preview-list"><h3>{label}</h3><p>{values.filter(Boolean).join(" · ") || missing}</p></div>;
}
