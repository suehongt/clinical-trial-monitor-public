/**
 * Shared presentational building blocks (Phase 3E): severity badges, change
 * values, empty states, the monitoring service-state banner and small
 * formatting helpers.  Restrained blue/gray clinical style — no marketing
 * flash; status is never conveyed by color alone (badges carry text).
 */

import { forwardRef, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";
import type { Severity } from "./types";
import type { Translate } from "./i18n";
import { navigate } from "./router";
import { reprobe } from "./health";
import { formatChangeValue } from "./intelligenceState";
import { Icon, type IconName } from "./icons";
import { createPortal } from "react-dom";

const ORDER: Severity[] = ["critical", "important", "normal", "minor"];

export function asSeverity(v: string | null | undefined): Severity {
  return ORDER.includes(v as Severity) ? (v as Severity) : "normal";
}

export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger" | "icon";

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; loading?: boolean }>(function Button({ variant = "secondary", loading = false, className = "", children, disabled, ...props }, ref) {
  return <button ref={ref} className={`ui-button ui-button-${variant} ${className}`.trim()} disabled={disabled || loading} aria-busy={loading || undefined} {...props}>
    {loading && <span className="ui-button-spinner" aria-hidden="true" />}{children}
  </button>;
});

export const IconButton = forwardRef<HTMLButtonElement, Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "title"> & { label: string; title?: string; icon: IconName; badge?: string | number }>(function IconButton({ label, title, icon, badge, className = "", ...props }, ref) {
  return <button ref={ref} className={`ui-icon-button ${className}`.trim()} aria-label={label} title={title ?? label} {...props}>
    <Icon name={icon} />
    {badge !== undefined && badge !== 0 && <span className="ui-icon-badge bell-count" aria-hidden="true">{badge}</span>}
  </button>;
});

export function TextInput({ label, error, leading, trailing, className = "", id, ...props }:
  React.InputHTMLAttributes<HTMLInputElement> & { label: string; error?: string; leading?: ReactNode; trailing?: ReactNode }) {
  const generated = useId();
  const inputId = id ?? generated;
  const errorId = `${inputId}-error`;
  return <label className={`ui-field ${className}`.trim()}>
    <span className="ui-field-label">{label}</span>
    <span className={`ui-input-wrap ${error ? "ui-input-error" : ""}`}>{leading}<input id={inputId} aria-invalid={Boolean(error)} aria-describedby={error ? errorId : undefined} {...props} />{trailing}</span>
    {error && <span id={errorId} className="ui-field-error">{error}</span>}
  </label>;
}

export function Select({ label, children, className = "", id, ...props }:
  SelectHTMLAttributes<HTMLSelectElement> & { label: string; children: ReactNode }) {
  const generated = useId();
  return <label className={`ui-field ${className}`.trim()}><span className="ui-field-label">{label}</span><select id={id ?? generated} {...props}>{children}</select></label>;
}

export function SegmentedControl<T extends string>({ label, value, options, onChange, focusTarget }:
  { label: string; value: T; options: Array<{ value: T; label: string }>; onChange: (value: T) => void; focusTarget?: React.RefObject<HTMLElement | null> }) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const choose = (index: number) => {
    const next = options[(index + options.length) % options.length];
    onChange(next.value);
    refs.current[(index + options.length) % options.length]?.focus();
  };
  return <div className="ui-segmented" role="radiogroup" aria-label={label}>
    {options.map((option, index) => <button key={option.value} ref={(node) => { refs.current[index] = node; }} type="button" role="radio" aria-checked={value === option.value} tabIndex={value === option.value ? 0 : -1} className={value === option.value ? "is-selected" : ""} onClick={() => { onChange(option.value); focusTarget?.current?.focus(); }} onKeyDown={(event) => {
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); choose(index + 1); }
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); choose(index - 1); }
      if (event.key === "Home") { event.preventDefault(); choose(0); }
      if (event.key === "End") { event.preventDefault(); choose(options.length - 1); }
    }}>{option.label}</button>)}
  </div>;
}

export function Panel({ header, footer, children, className = "" }: { header?: ReactNode; footer?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`ui-panel ${className}`.trim()}>{header && <header className="ui-panel-header">{header}</header>}<div className="ui-panel-body">{children}</div>{footer && <footer className="ui-panel-footer">{footer}</footer>}</section>;
}

export function StatusBadge({ tone = "unknown", children }: { tone?: "positive" | "warning" | "critical" | "normal" | "unknown"; children: ReactNode }) {
  return <span className={`ui-status ui-status-${tone}`}><span aria-hidden="true" />{children}</span>;
}

export function RegistryBadge({ children, fullName }: { children: ReactNode; fullName?: string }) {
  return <span className="ui-registry" title={fullName}>{children}<span className="sr-only">{fullName && fullName !== children ? ` (${fullName})` : ""}</span></span>;
}

export function FreshnessIndicator({ state, date }: { state: "fresh" | "delayed" | "stale" | "unknown"; date?: string }) {
  const label = state[0].toUpperCase() + state.slice(1);
  return <span className={`ui-freshness ui-freshness-${state}`} title={date ? `${label} · ${date}` : label}><span aria-hidden="true" />{label}{date && <small>{date}</small>}</span>;
}

export function Drawer({ open, label, onClose, triggerRef, children, mobile = "bottom" }:
  { open: boolean; label: string; onClose: () => void; triggerRef?: React.RefObject<HTMLElement | null>; children: ReactNode; mobile?: "bottom" | "full" }) {
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const root = document.querySelector<HTMLElement>("#root");
    const workspace = root?.firstElementChild as HTMLElement | null;
    workspace?.setAttribute("inert", "");
    const focusable = () => Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? []);
    requestAnimationFrame(() => focusable()[0]?.focus());
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab") return;
      const nodes = focusable();
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = previousOverflow;
      workspace?.removeAttribute("inert");
      triggerRef?.current?.focus();
    };
  }, [open, onClose, triggerRef]);
  if (!open) return null;
  return createPortal(<div className="ui-drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><div ref={panelRef} className={`ui-drawer ui-drawer-${mobile}`} role="dialog" aria-modal="true" aria-label={label}>{children}</div></div>, document.body);
}

export function Dialog({ open, title, description, onClose, triggerRef, children, className = "" }: {
  open: boolean; title: string; description?: string; onClose: () => void;
  triggerRef?: React.RefObject<HTMLElement | null>; children: ReactNode; className?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable = () => Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? []);
    requestAnimationFrame(() => focusable()[0]?.focus());
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab") return;
      const nodes = focusable();
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); document.body.style.overflow = previousOverflow; triggerRef?.current?.focus(); };
  }, [open, onClose, triggerRef]);
  if (!open) return null;
  return createPortal(<div className="ui-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={panelRef} className={`ui-dialog ${className}`.trim()} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined}>
      <header className="ui-dialog-header"><div><h2 id={titleId}>{title}</h2>{description && <p id={descriptionId}>{description}</p>}</div></header>
      {children}
    </div>
  </div>, document.body);
}

export function ConfirmDialog({ open, title, description, confirmLabel, cancelLabel, busy, error, onConfirm, onClose, triggerRef }: {
  open: boolean; title: string; description: string; confirmLabel: string; cancelLabel: string;
  busy?: boolean; error?: string; onConfirm: () => void; onClose: () => void; triggerRef?: React.RefObject<HTMLElement | null>;
}) {
  return <Dialog open={open} title={title} description={description} onClose={() => { if (!busy) onClose(); }} triggerRef={triggerRef} className="ui-confirm-dialog">
    {error && <InlineAlert tone="error" title={error} />}
    <footer className="ui-dialog-actions">
      <Button variant="secondary" autoFocus disabled={busy} onClick={onClose}>{cancelLabel}</Button>
      <Button variant="danger" loading={busy} onClick={onConfirm}>{confirmLabel}</Button>
    </footer>
  </Dialog>;
}

export function ActionMenu({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent | MouseEvent) => {
      if (event instanceof KeyboardEvent && event.key === "Escape") { setOpen(false); root.current?.querySelector<HTMLButtonElement>("button")?.focus(); }
      if (event instanceof MouseEvent && !root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", close); document.addEventListener("mousedown", close);
    return () => { document.removeEventListener("keydown", close); document.removeEventListener("mousedown", close); };
  }, [open]);
  return <div className="ui-action-menu" ref={root}>
    <button type="button" className="ui-button ui-button-quiet" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{label}</button>
    {open && <div className="ui-action-menu-popover" role="menu" onClick={() => setOpen(false)}>{children}</div>}
  </div>;
}

export function Skeleton({ label = "Loading", lines = 3 }: { label?: string; lines?: number }) {
  return <div className="ui-skeleton" role="status" aria-busy="true"><span className="sr-only">{label}</span>{Array.from({ length: lines }, (_, i) => <span key={i} />)}</div>;
}

export function InlineAlert({ tone = "info", title, children, action }: { tone?: "info" | "warning" | "error" | "success"; title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className={`ui-alert ui-alert-${tone}`} role={tone === "error" ? "alert" : "status"}><div><strong>{title}</strong>{children && <div>{children}</div>}</div>{action}</div>;
}

/** Severity badge — text-carrying, so importance never rides on color alone. */
export function SeverityBadge({ severity, t }: { severity?: string | null; t: Translate }) {
  const s = asSeverity(severity);
  return <span className={`sev sev-${s}`}>{t(`sev.${s}` as never)}</span>;
}

/** Old/new value cell: long values collapse behind a show-more toggle. */
export function ChangeValue({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  const text = formatChangeValue(value, "—");
  const long = text.length > 180;
  return (
    <span className="change-value">
      {long && !open ? text.slice(0, 180) + "…" : text}
      {long && <button type="button" className="text-btn" onClick={() => setOpen(!open)}>{open ? "−" : "+"}</button>}
    </span>
  );
}

/** Deliberate empty state (icon-free, quiet, one sentence). */
export function EmptyState({ title, hint, children }: { title: string; hint?: string; children?: React.ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
      {children}
    </div>
  );
}

export function Loading({ label }: { label: string }) {
  return <div className="empty-state"><div className="empty-hint">{label}</div></div>;
}

/**
 * Progress feedback for the search loading state: an indeterminate animated
 * bar plus an elapsed-seconds counter, so a slow search reads as "working",
 * not "hung".  The total duration is unknowable client-side (server-side
 * query + optional live-source work), hence indeterminate rather than a
 * percentage; the counter is the honest part.  After SLOW_AFTER_SECONDS a
 * calm hint appears instead of silence.
 */
const SLOW_AFTER_SECONDS = 10;

export function SearchProgress({ label, t }: { label: string; t: Translate }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const started = Date.now();
    setElapsed(0);
    const id = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, []);
  return (
    <div className="search-progress" role="status">
      <div className="progress-track" aria-hidden="true"><div className="progress-fill" /></div>
      <div className="search-progress-line">
        <span>{label}</span>
        <span className="search-progress-elapsed">{t("search.elapsed", { n: elapsed })}</span>
      </div>
      {elapsed >= SLOW_AFTER_SECONDS && (
        <p className="form-hint">{t("search.slowHint")}</p>
      )}
    </div>
  );
}

export function ErrorNote({ message, onRetry, retryLabel }: { message: string; onRetry?: () => void; retryLabel?: string }) {
  return (
    <div className="error-note" role="alert">
      <span>{message}</span>
      {onRetry && <button type="button" className="text-btn" onClick={onRetry}>{retryLabel ?? "Retry"}</button>}
    </div>
  );
}

/**
 * Monitoring service-state banner (replaces the old dead-end
 * "Monitoring features require the local server." message): states what is
 * unavailable, keeps trial browsing available, offers Retry, and in dev
 * builds shows the one-line startup command.  Never exposes stack traces.
 */
export function ServiceUnavailable({ health, t }: { health: { state: string; error: string | null }; t: Translate }) {
  const [retrying, setRetrying] = useState(false);
  const retry = () => {
    setRetrying(true);
    reprobe().finally(() => setRetrying(false));
  };
  const dbBroken = health.state === "database_error";
  return (
    <div className={`service-banner ${dbBroken ? "service-banner-db" : ""}`} role="alert">
      <div className="service-banner-head">{t("service.title")}</div>
      <p className="service-banner-body">{dbBroken ? t("service.dbBody") : t("service.body")}</p>
      <dl className="service-status">
        <div><dt>API</dt><dd>{dbBroken ? t("service.apiDegraded") : t("service.apiOffline")}</dd></div>
        <div><dt>{t("service.dbLabel")}</dt><dd>{dbBroken ? t("service.dbUnavailable") : t("service.dbUnknown")}</dd></div>
      </dl>
      <div className="service-actions">
        <button type="button" className="chip chip-on" onClick={retry} disabled={retrying}>
          {retrying ? t("service.retrying") : t("service.retry")}
        </button>
        <button type="button" className="chip" onClick={() => navigate("/trials")}>{t("service.browseTrials")}</button>
      </div>
      {import.meta.env.DEV && !dbBroken && (
        <p className="service-dev-hint">{t("service.devHint")}</p>
      )}
    </div>
  );
}

/** Internal link to a trial detail (SPA navigation, keeps scroll at top). */
export function TrialLink({ source, id, title, className, children }: {
  source: string; id: string; title?: string; className?: string; children?: React.ReactNode;
}) {
  return (
    <a
      className={className ?? "trial-link"}
      href={`/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}`}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        navigate(`/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}`);
      }}
    >
      {children ?? title ?? id}
    </a>
  );
}

/** "2 h ago" style relative stamp with absolute fallback in the title. */
export function relTime(stamp: string | null | undefined): string {
  if (!stamp) return "—";
  const ms = Date.parse(stamp.includes("T") ? stamp : stamp.replace(" ", "T") + "Z");
  if (Number.isNaN(ms)) return stamp;
  const diff = Date.now() - ms;
  if (diff < 0) return stamp.slice(0, 10);
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return stamp.slice(0, 10);
}

/** Absolute stamp in the viewer's timezone (backend timestamps are UTC). */
export function absTime(stamp: string | null | undefined): string {
  if (!stamp) return "—";
  const ms = Date.parse(stamp.includes("T") ? stamp : stamp.replace(" ", "T") + "Z");
  if (Number.isNaN(ms)) return stamp;
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}
