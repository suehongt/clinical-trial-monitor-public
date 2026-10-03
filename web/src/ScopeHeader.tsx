import { FreshnessBadge } from "./IntelligenceViz";
import type { Translate } from "./i18n";

export type ScopeChoice = { value: string; label: string };

export default function ScopeHeader({ eyebrow, title, description, meta, choices, selected, onSelect, sources, children, level = 1, t }: {
  eyebrow: string; title: string; description?: string; meta?: string;
  t: Translate;
  level?: 1 | 2;
  choices?: ScopeChoice[]; selected?: string; onSelect?: (value: string) => void;
  sources?: Array<{ name: string; freshness: string }>;
  children?: React.ReactNode;
}) {
  return <header className="scope-header">
    <div className="scope-top"><div><span className="scope-eyebrow">{eyebrow}</span>{level === 1 ? <h1>{title}</h1> : <h2>{title}</h2>}{description && <p>{description}</p>}{meta && <small>{meta}</small>}</div>
      {choices && <label className="scope-picker"><span className="sr-only">{t("ws.scope")}</span><select aria-label={t("ws.scope")} value={selected} onChange={(e) => onSelect?.(e.target.value)}>{choices.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>}
    </div>
    {(children || !!sources?.length) && <div className="scope-bottom">{children && <div className="scope-filters">{children}</div>}{!!sources?.length && <div className="scope-sources">{sources.map((s) => <span key={s.name}>{s.name} <FreshnessBadge freshness={s.freshness} t={t} /></span>)}</div>}</div>}
  </header>;
}
