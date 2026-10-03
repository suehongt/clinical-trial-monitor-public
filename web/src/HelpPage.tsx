import { useState } from "react";
import type { Lang } from "./i18n";
import { navigate, useRoute } from "./router";
import { localize, releaseNotes, searchHelp, startHere, userGuide, type ChangeType } from "./helpContent";
import { EmptyState } from "./ui";

const CHANGE_LABELS: Record<ChangeType, Record<Lang, string>> = { feature: { zh: "新增", en: "New" }, improvement: { zh: "优化", en: "Improved" }, fix: { zh: "修复", en: "Fixed" } };
type Tab = "start" | "guide" | "releases";
const tabOf = (value: string | null): Tab => value === "guide" || value === "releases" ? value : "start";

export default function HelpPage({ lang }: { lang: Lang }) {
  const route = useRoute();
  const tab = tabOf(route.query.get("tab"));
  const query = route.query.get("q") ?? "";
  const [draft, setDraft] = useState(query);
  const sections = searchHelp(lang, query);
  const go = (next: Tab, params = "") => navigate(`/help?tab=${next}${params}`);
  return <section className="page-main help-page">
    <header className="help-hero"><p className="help-eyebrow">{lang === "zh" ? "帮助中心" : "Help center"}</p><h1>{lang === "zh" ? "从检索到研究简报" : "From search to research briefing"}</h1><p>{lang === "zh" ? "理解核心概念、完成常见工作流并查看版本变化。" : "Learn the core concepts, complete common workflows and review product changes."}</p></header>
    <nav className="page-tabs help-tabs" role="tablist" aria-label={lang === "zh" ? "帮助内容" : "Help content"}>{(["start", "guide", "releases"] as Tab[]).map((value) => <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? "tab-on" : ""} onClick={() => go(value)}>{value === "start" ? localize(startHere.title, lang) : value === "guide" ? localize(userGuide.title, lang) : localize(releaseNotes.title, lang)}</button>)}</nav>
    {tab === "start" && <>
      <div className="help-intro"><div><h2>{localize(startHere.title, lang)}</h2><p>{localize(startHere.summary, lang)}</p></div></div>
      <ol className="start-steps">{startHere.steps.map((step, index) => <li key={step.path}><span className="step-number">{index + 1}</span><div><h3>{localize(step.title, lang)}</h3><p>{localize(step.body, lang)}</p><a href={step.path} onClick={(event) => { event.preventDefault(); navigate(step.path); }}>{lang === "zh" ? "打开" : "Open"} →</a></div></li>)}</ol>
      <section className="guide-section"><h2>{lang === "zh" ? "核心概念" : "Core concepts"}</h2><div className="concept-grid">{startHere.concepts.map((item) => <article className="guide-card" key={item.term}><h3>{item.term}</h3><p>{localize(item.body, lang)}</p></article>)}</div></section>
      <section className="guide-section"><h2>{lang === "zh" ? "状态解释" : "Status guide"}</h2><div className="guide-card-grid">{startHere.statuses.map((item) => <article className="guide-card" key={item.title.en}><h3>{localize(item.title, lang)}</h3><p>{localize(item.body, lang)}</p></article>)}</div></section>
    </>}
    {tab === "guide" && <>
      <div className="help-intro"><div><h2>{localize(userGuide.title, lang)}</h2><p>{localize(userGuide.summary, lang)}</p></div><small>{lang === "zh" ? `更新于 ${userGuide.updatedAt}` : `Updated ${userGuide.updatedAt}`}</small></div>
      <form className="help-search" role="search" onSubmit={(event) => { event.preventDefault(); go("guide", draft.trim() ? `&q=${encodeURIComponent(draft.trim())}` : ""); }}><label><span>{lang === "zh" ? "搜索帮助" : "Search help"}</span><input value={draft} onChange={(event) => setDraft(event.target.value)} /></label><button type="submit">{lang === "zh" ? "搜索" : "Search"}</button>{query && <button type="button" className="text-btn" onClick={() => { setDraft(""); go("guide"); }}>{lang === "zh" ? "清除" : "Clear"}</button>}</form>
      {sections.length === 0 && <EmptyState title={lang === "zh" ? "未找到帮助内容" : "No help results"} hint={lang === "zh" ? "请尝试更宽泛的关键词。" : "Try a broader term."} />}
      <div className="guide-sections">{sections.map((section, index) => <section className="guide-section" id={section.id} key={section.id}><div className="guide-section-heading"><span>{String(index + 1).padStart(2, "0")}</span><div><h2>{localize(section.title, lang)}</h2><p>{localize(section.description, lang)}</p></div></div><div className="guide-card-grid">{section.items.map((item) => <article className="guide-card" key={item.title.en}><h3>{localize(item.title, lang)}</h3><p>{localize(item.body, lang)}</p>{item.link && <a href={item.link.path} onClick={(event) => { event.preventDefault(); navigate(item.link!.path); }}>{localize(item.link.label, lang)} →</a>}</article>)}</div></section>)}</div>
    </>}
    {tab === "releases" && <><div className="help-intro"><div><h2>{localize(releaseNotes.title, lang)}</h2><p>{localize(releaseNotes.summary, lang)}</p></div><small>{lang === "zh" ? `更新于 ${releaseNotes.updatedAt}` : `Updated ${releaseNotes.updatedAt}`}</small></div><div className="release-list">{releaseNotes.releases.map((release) => <article className="release-card" key={release.version}><header><div><span className="release-version">v{release.version}</span>{release.status === "current" && <span className="release-current">{lang === "zh" ? "当前版本" : "Current"}</span>}<h2>{localize(release.title, lang)}</h2></div><time dateTime={release.date}>{release.date}</time></header><ul>{release.changes.map((change, index) => <li key={`${change.type}-${index}`}><span className={`change-kind change-${change.type}`}>{CHANGE_LABELS[change.type][lang]}</span><span>{localize(change.text, lang)}</span></li>)}</ul></article>)}</div></>}
  </section>;
}
