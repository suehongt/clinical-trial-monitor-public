import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import ErrorBoundary from "./ErrorBoundary";
import { useBackendHealth } from "./health";
import { lastRoute, navigate, redirectPath, rememberRoute, useRoute } from "./router";
import { translate, type Lang, type Translate } from "./i18n";
import { refreshUnreadCount } from "./notificationState";
import DashboardPage from "./DashboardPage";
import TrialsPage from "./TrialsPage";
import { LazyRouteBoundary, RouteLoading } from "./LazyRouteBoundary";
import { Drawer, EmptyState, IconButton, SegmentedControl } from "./ui";
import { Icon, type IconName } from "./icons";
import { buildGlobalSearchUrl, globalSearchFromQuery, navKeyFor, persistedLang, persistedSidebar, persistedTheme, type NavKey, type SearchMode } from "./shellState";

const TrialDetailPage = lazy(() => import("./TrialDetailPage"));
const MonitorsPage = lazy(() => import("./MonitorsPage"));
const UpdatesPage = lazy(() => import("./UpdatesPage"));
const DataSourcesPage = lazy(() => import("./DataSourcesPage"));
const BriefingPage = lazy(() => import("./BriefingPage"));
const ProjectsPage = lazy(() => import("./ProjectsPage"));
const HelpPage = lazy(() => import("./HelpPage"));

const NAV: Array<{ group: string; items: Array<{ key: NavKey; path: string; icon: IconName; labelKey: Parameters<Translate>[0] }> }> = [
  { group: "Overview", items: [{ key: "dashboard", path: "/dashboard", icon: "dashboard", labelKey: "nav.dashboard" }] },
  { group: "Research", items: [
    { key: "trials", path: "/trials", icon: "trials", labelKey: "nav.trials" },
    { key: "projects", path: "/projects", icon: "projects", labelKey: "nav.projects" },
    { key: "saved", path: "/trials?view=saved", icon: "saved", labelKey: "saved.title" },
  ] },
  { group: "Monitoring", items: [
    { key: "monitors", path: "/monitors", icon: "monitors", labelKey: "nav.monitors" },
    { key: "updates", path: "/updates", icon: "updates", labelKey: "nav.updates" },
  ] },
  { group: "Intelligence", items: [{ key: "briefing", path: "/briefing", icon: "briefing", labelKey: "intel.briefing" }] },
  { group: "System", items: [
    { key: "sources", path: "/data-sources", icon: "sources", labelKey: "nav.dataSources" },
    { key: "help", path: "/help", icon: "help", labelKey: "nav.help" },
  ] },
];

function useUnreadCount(online: boolean, url: string): [number, (n: number) => void] {
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!online) { setN(0); return; }
    let alive = true;
    const load = () => refreshUnreadCount().then((count) => { if (alive) setN(count); }).catch(() => undefined);
    load();
    const interval = window.setInterval(load, 60_000);
    return () => { alive = false; window.clearInterval(interval); };
  }, [online, url]);
  return [n, setN];
}

function Shell() {
  const route = useRoute();
  const health = useBackendHealth();
  const [theme, setTheme] = useState(() => persistedTheme(localStorage.getItem("ct-theme")));
  const [lang, setLang] = useState<Lang>(() => persistedLang(localStorage.getItem("ct-lang")));
  const [unread, setUnread] = useUnreadCount(health.state === "online", route.url);
  const [menuOpen, setMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => persistedSidebar(localStorage.getItem("ct-sidebar")) === "collapsed");
  const initialSearch = globalSearchFromQuery(route.query);
  const [globalQuery, setGlobalQuery] = useState(initialSearch.value);
  const [searchMode, setSearchMode] = useState<SearchMode>(initialSearch.mode);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const closeMenu = useCallback(() => setMenuOpen(false), []);

  useEffect(() => { setMenuOpen(false); }, [route.url]);
  useEffect(() => { localStorage.setItem("ct-sidebar", collapsed ? "collapsed" : "expanded"); }, [collapsed]);
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem("ct-theme", theme); }, [theme]);
  useEffect(() => { document.documentElement.lang = lang === "zh" ? "zh-CN" : "en"; localStorage.setItem("ct-lang", lang); }, [lang]);
  useEffect(() => { const next = globalSearchFromQuery(route.query); setGlobalQuery(next.value); setSearchMode(next.mode); }, [route.url]);

  const t = useMemo<Translate>(() => (key, vars) => translate(lang, key, vars), [lang]);
  const lastUrl = useRef<string | null>(null);
  if (lastUrl.current !== route.url) {
    lastUrl.current = route.url;
    const target = route.path === "/" ? (lastRoute() ?? redirectPath(route.path, route.query)) : redirectPath(route.path, route.query);
    if (target) { navigate(target, true); lastUrl.current = target; return null; }
    rememberRoute(route.url);
  }

  const active = navKeyFor(route.path, `?${route.query.toString()}`);
  const submitGlobalSearch = (event: FormEvent) => { event.preventDefault(); navigate(buildGlobalSearchUrl(globalQuery, searchMode)); };
  const follow = (event: React.MouseEvent<HTMLAnchorElement>, path: string) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault(); navigate(path); closeMenu();
  };
  const nav = (mobile = false) => <nav className="shell-nav" aria-label={t("a11y.mainNav")}>
    {NAV.map((group) => <section className="shell-nav-group" key={group.group}>
      <div className="shell-nav-heading">{t(`ws.nav.${group.group.toLowerCase()}` as Parameters<Translate>[0])}</div>
      {group.items.map((item) => <a key={item.key} className={`shell-nav-link ${active === item.key ? "is-active" : ""}`} aria-current={active === item.key ? "page" : undefined} href={item.path} title={collapsed && !mobile ? t(item.labelKey) : undefined} onClick={(event) => follow(event, item.path)}><Icon name={item.icon} /><span>{t(item.labelKey)}</span></a>)}
    </section>)}
  </nav>;

  let page: React.ReactNode;
  if (route.segments[0] === "dashboard" || route.segments.length === 0) page = <DashboardPage t={t} lang={lang} health={health} />;
  else if (route.segments[0] === "trials" && route.segments.length >= 3) page = <TrialDetailPage t={t} lang={lang} health={health} />;
  else if (route.segments[0] === "trials") page = <TrialsPage t={t} lang={lang} dataSource={health.state === "online" ? "api" : "static"} />;
  else if (route.segments[0] === "monitors") page = <MonitorsPage t={t} lang={lang} health={health} />;
  else if (route.segments[0] === "projects") page = <ProjectsPage t={t} lang={lang} health={health} />;
  else if (route.segments[0] === "updates") page = <UpdatesPage t={t} lang={lang} health={health} onUnreadCount={setUnread} />;
  else if (route.segments[0] === "data-sources") page = <DataSourcesPage t={t} health={health} />;
  else if (route.segments[0] === "briefing") page = <BriefingPage t={t} lang={lang} health={health} />;
  else if (route.segments[0] === "help") page = <HelpPage lang={lang} />;
  else page = <EmptyState title={t("err.routeNotFound")} hint={t("err.routeNotFoundHint")} />;

  return <div className={`app workspace shell ${collapsed ? "shell-is-collapsed" : ""}`}>
    <header className="shell-topbar">
      <IconButton ref={menuButtonRef} className="shell-menu-button" label={menuOpen ? t("shell.closeMenu") : t("shell.openMenu")} icon={menuOpen ? "close" : "menu"} aria-expanded={menuOpen} onClick={() => setMenuOpen((value) => !value)} />
      <a className="shell-brand" href="/dashboard" aria-label={`${t("app.title")} · ${t("nav.dashboard")}`} onClick={(event) => follow(event, "/dashboard")}><span className="shell-brand-mark"><Icon name="brand" /></span><span>{t("app.title")}</span></a>
      <form className="shell-search" role="search" onSubmit={submitGlobalSearch}>
        <SegmentedControl label={t("shell.searchMode")} value={searchMode} onChange={setSearchMode} focusTarget={searchInputRef} options={[{ value: "keyword", label: t("shell.keyword") }, { value: "natural", label: t("nl.askMode") }]} />
        <label className="shell-search-field"><span className="sr-only">{searchMode === "natural" ? t("shell.askLabel") : t("a11y.globalSearch")}</span><Icon name="search" /><input ref={searchInputRef} id="global-trial-search" value={globalQuery} aria-label={searchMode === "natural" ? t("shell.askLabel") : t("a11y.globalSearch")} placeholder={searchMode === "natural" ? t("shell.askPlaceholder") : t("search.globalPlaceholder")} onChange={(event) => setGlobalQuery(event.target.value)} /><button type="submit">{t("search.submit")}</button></label>
      </form>
      <div className="shell-actions">
        {health.state === "online" && <IconButton label={t("a11y.notifications")} title={t("nav.notifications")} icon="bell" badge={unread > 99 ? "99+" : unread || undefined} onClick={() => navigate("/updates?view=notifications")} />}
        <IconButton label={theme === "dark" ? t("shell.useLight") : t("shell.useDark")} icon={theme === "dark" ? "sun" : "moon"} onClick={() => setTheme(theme === "dark" ? "light" : "dark")} />
        <button type="button" className="shell-language" aria-label={t("a11y.lang")} onClick={() => setLang(lang === "en" ? "zh" : "en")}><Icon name="language" /><span>{lang === "en" ? "中文" : "EN"}</span></button>
      </div>
    </header>
    <aside className="shell-sidebar">{nav()}<button type="button" className="shell-collapse" aria-label={collapsed ? t("shell.expandSidebar") : t("shell.collapseSidebar")} title={collapsed ? t("shell.expandSidebar") : undefined} onClick={() => setCollapsed((value) => !value)}><Icon name={collapsed ? "expand" : "collapse"} /><span>{collapsed ? t("shell.expandSidebar") : t("shell.collapseSidebar")}</span></button></aside>
    <main className="main-canvas" id="main-content"><LazyRouteBoundary key={route.path} t={t}><Suspense fallback={<RouteLoading t={t} />}>{page}</Suspense></LazyRouteBoundary></main>
    <Drawer open={menuOpen} label={t("shell.navigation")} onClose={closeMenu} triggerRef={menuButtonRef} mobile="full"><div className="shell-mobile-drawer"><header><strong>{t("shell.navigation")}</strong><IconButton label={t("shell.closeMenu")} icon="close" onClick={closeMenu} /></header>{nav(true)}</div></Drawer>
  </div>;
}

export default function App() { return <ErrorBoundary><Shell /></ErrorBoundary>; }
