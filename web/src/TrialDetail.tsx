import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, EndpointRow, NctStudyCheck, Trial, TrialDetailData } from "./types";
import { SOURCE_BADGES, statusTone } from "./labels";
import { fetchJson } from "./api";
import { formatChangeValue } from "./intelligenceState";
import { isNctStudyCheck } from "./guards";
import type { Translate } from "./i18n";
import { safeExternalUrl } from "./urlSafety";

function fmtEnrollment(n?: number | null): string {
  return typeof n === "number" ? n.toLocaleString() : "—";
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="d-field">
      <div className="d-label">{label}</div>
      <div className="d-value">{children}</div>
    </div>
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
  // A single multi-line block (flattened CDE endpoint tables) renders
  // pre-line; clean short items render as a bullet list.
  const isBlock = items.length === 1 && items[0].includes("\n");
  return (
    <div className="d-field">
      <div className="d-label">{title}</div>
      <div className="d-value">
        {isBlock
          ? <div className="d-pre">{items[0]}</div>
          : <ul className="d-list">{items.map((s, i) => <li key={i}>{s}</li>)}</ul>}
      </div>
    </div>
  );
}

export default function TrialDetail({ trial, detail, changeHistory, firstCrawledAt, liveCheckUrl, detailState, detailError, onClose, watching = false, onToggleWatch, t }:
  { trial: Trial; detail?: TrialDetailData | null;
    changeHistory?: ChangeEvent[] | null;
    firstCrawledAt?: string | null;
    liveCheckUrl?: string | null;
    detailState: "loading" | "ready" | "missing";
    detailError?: string | null; onClose: () => void; watching?: boolean;
    onToggleWatch?: () => void; t: Translate }) {
  // LIVE single-trial check (NCT, API mode): on-demand compare against the
  // registry, local component state — one fetch per button press
  type CheckState =
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ready"; data: NctStudyCheck }
    | { status: "error"; message: string };
  const [check, setCheck] = useState<CheckState>({ status: "idle" });
  const runCheck = () => {
    if (!liveCheckUrl) return;
    setCheck({ status: "loading" });
    fetchJson<NctStudyCheck>(liveCheckUrl, isNctStudyCheck)
      .then((data) => setCheck({ status: "ready", data }))
      .catch((e: unknown) => setCheck({
        status: "error",
        message: e instanceof Error ? e.message : String(e),
      }));
  };
  const dialogRef = useRef<HTMLDivElement | null>(null);

  // a11y focus management, mount-only (deliberately NOT keyed on onClose so a
  // re-created callback cannot re-capture focus state): on open, remember the
  // element that had focus and move focus into the dialog; on unmount, the
  // cleanup returns focus to that element.
  useEffect(() => {
    const prevFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();
    return () => { prevFocus?.focus(); };
  }, []);

  // ESC to close + body scroll lock
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const badge = SOURCE_BADGES[trial.source] ?? { label: trial.source, color: "#64748b" };
  const sponsors = trial.sponsors.filter(Boolean);
  const sites = trial.locations.filter(Boolean);
  const interventions = trial.interventions.filter(Boolean);
  const detailReady = detailState === "ready";
  const table = detail?.endpointTable;
  const meta: Array<[string, React.ReactNode]> = [
    [t("field.studyType"), trial.studyType ?? "—"],
    [t("field.phase"), trial.phase && trial.phase !== "N/A" ? trial.phase : "—"],
    [t("field.startDate"), trial.startDate ?? "—"],
    [t("field.enrollment"), fmtEnrollment(trial.enrollment)],
    [t("field.completionDate"), trial.completionDate ?? "—"],
    [t("field.lastUpdated"), trial.lastUpdated ?? "—"],
    [t("field.registration"), trial.registrationDate ?? "—"],
    [t("field.countries"), trial.countries.length ? trial.countries.join(", ") : "—"],
    [t("field.intervention"), interventions.length ? interventions.join(" · ") : "—"],
    [t("field.investigator"), detail?.investigator ?? "—"],
    [t("field.sponsor"), sponsors.length ? sponsors.join(" · ") : "—"],
  ];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="trial-detail-title"
           tabIndex={-1} ref={dialogRef} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="modal-head-left">
            <span className="src-badge" style={{ backgroundColor: badge.color }}>{badge.label}</span>
            <span className={`pill ${statusTone(trial.status)}`}>{trial.status}</span>
            <span className="tid">{trial.id}</span>
          </div>
          <div className="modal-head-right">
            {onToggleWatch && <button className="check-btn" onClick={onToggleWatch}
              aria-pressed={watching}>{watching ? t("trial.watching") : t("trial.watch")}</button>}
            {liveCheckUrl && (
              <button className="check-btn" onClick={runCheck}
                      title={t("check.loading")}>{t("check.button")}</button>
            )}
            {safeExternalUrl(trial.url) && <a className="reg-link" href={safeExternalUrl(trial.url)!} target="_blank" rel="noreferrer">{t("link.registry")}</a>}
            <button className="close" onClick={onClose} aria-label={t("a11y.close")}>✕</button>
          </div>
        </div>

        <div className="modal-body">
          <h2 className="d-title" id="trial-detail-title">{trial.title}</h2>
          {trial.scientificTitle && trial.scientificTitle !== trial.title && (
            <p className="d-sci">{trial.scientificTitle}</p>
          )}

          <div className="d-grid">
            {meta.map(([label, value]) => <Field key={label} label={label}>{value}</Field>)}
          </div>

          {liveCheckUrl && check.status !== "idle" && (
            <div className="check-result">
              {check.status === "loading" && <div className="d-loading">{t("check.loading")}</div>}
              {check.status === "error" && <div className="d-loading">{t("live.error")}{check.message}</div>}
              {check.status === "ready" && (
                <>
                  {!check.data.in_library && <div className="d-loading">{t("check.notInLib")}</div>}
                  {check.data.in_library && check.data.diff.length === 0 && (
                    <div className="d-loading">{t("check.same")}</div>
                  )}
                  {check.data.diff.length > 0 && (
                    <>
                      <div className="d-label">{t("check.diffTitle")}</div>
                      <div className="tc-diffs">
                        {check.data.diff.map((d, i) => (
                          <div className="diff-row" key={i}>
                            <span className="diff-field">{d.field}</span>
                            <span className="diff-old">{String(d.local ?? "—")}</span>
                            <span className="diff-arrow">→</span>
                            <span className="diff-new">{String(d.remote ?? "—")}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </>
              )}
            </div>
          )}

          {detailState === "loading" && <div className="d-loading">{t("detail.loading")}</div>}
          {detailState === "missing" && (
            <div className="d-loading">{t("detail.missing")}</div>
          )}
          {detailError && <div className="d-loading">{t("error.details")}{detailError}</div>}

          {detailReady && (
            <>
              {detail?.summary && (
                <div className="d-field">
                  <div className="d-label">{t("field.purpose")}</div>
                  <div className="d-value d-summary">{detail.summary}</div>
                </div>
              )}

              {trial.conditions.length > 0 && (
                <div className="d-field">
                  <div className="d-label">{t("field.conditions")}</div>
                  <div className="d-value tags">
                    {trial.conditions.map((c, i) => <span className="tag" key={i}>{c}</span>)}
                  </div>
                </div>
              )}

              {Array.isArray(changeHistory) && (() => {
                // The trial's change timeline — always rendered (API mode):
                // an "added to monitor" origin node keeps it meaningful even
                // for freshly registered trials with no changes yet, and the
                // version-chain events below answer "when did what change".
                const byDate = new Map<string, typeof changeHistory>();
                for (const ev of changeHistory) {
                  const d = (ev.detected_at || "").slice(0, 10) || "—";
                  if (!byDate.has(d)) byDate.set(d, []);
                  byDate.get(d)!.push(ev);
                }
                return (
                  <div className="d-field">
                    <div className="d-label">{t("field.changeHistory")}</div>
                    <div className="d-value">
                      <div className="tl">
                        <div className="tl-day">
                          <div className="tl-rail">
                            <span className="tl-dot tl-dot-origin" />
                            <span className="tl-line" />
                          </div>
                          <div className="tl-body">
                            <div className="tl-date">{t("timeline.registered")}</div>
                            <div className="tl-sub">
                              {firstCrawledAt ? firstCrawledAt.slice(0, 10) : "—"}
                            </div>
                          </div>
                        </div>
                        {changeHistory.length === 0 && (
                          <div className="tl-nochange">{t("timeline.noChanges")}</div>
                        )}
                        {[...byDate.entries()].map(([date, evs]) => (
                          <div className="tl-day" key={date}>
                            <div className="tl-rail">
                              <span className="tl-dot" />
                              <span className="tl-line" />
                            </div>
                            <div className="tl-body">
                              <div className="tl-date">{date}</div>
                              <div className="tc-diffs">
                                {evs.map((ev, i) => (
                                  <div className="diff-row" key={i}>
                                    <span className="diff-field">{ev.field_name}</span>
                                    <span className="diff-time">{(ev.detected_at || "").slice(11, 16)}</span>
                                    <span className="diff-old">{formatChangeValue(ev.old_value, "—")}</span>
                                    <span className="diff-arrow">→</span>
                                    <span className="diff-new">{formatChangeValue(ev.new_value, "—")}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                );
              })()}

              <div className="d-field">
                <div className="d-label">{t("field.primaryEndpoint")}</div>
                <div className="d-value">
                  {table?.primary?.length
                    ? <EndpointTable rows={table.primary} t={t} />
                    : <div className="d-pre">{trial.primaryEndpoint ?? "—"}</div>}
                </div>
              </div>
              {table?.secondary?.length
                ? <div className="d-field">
                    <div className="d-label">{t("field.secondaryEndpoints")}</div>
                    <div className="d-value"><EndpointTable rows={table.secondary} t={t} /></div>
                  </div>
                : <Endpoints title={t("field.secondaryEndpoints")} items={trial.secondaryEndpoints} />}

              {sites.length > 0 && (
                <div className="d-field">
                  <div className="d-label">{t("field.sites")}</div>
                  <div className="d-value">
                    <div className="site-list">
                      {sites.map((s: string, i: number) => <span className="site-item" key={i}>{s}</span>)}
                    </div>
                  </div>
                </div>
              )}

              {(detail?.inclusion || detail?.exclusion) && (
                <div className="d-criteria">
                  {detail?.inclusion && (
                    <div className="criteria-box criteria-in">
                      <div className="criteria-head">{t("criteria.inclusion")}</div>
                      <div className="criteria-body">{detail.inclusion}</div>
                    </div>
                  )}
                  {detail?.exclusion && (
                    <div className="criteria-box criteria-ex">
                      <div className="criteria-head">{t("criteria.exclusion")}</div>
                      <div className="criteria-body">{detail.exclusion}</div>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
