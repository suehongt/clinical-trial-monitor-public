export type NavKey = "dashboard" | "projects" | "trials" | "saved" | "monitors" | "updates" | "briefing" | "sources" | "help";
export type SearchMode = "keyword" | "natural";

export function navKeyFor(path: string, search = ""): NavKey {
  const query = new URLSearchParams(search);
  if (path.startsWith("/briefing")) return "briefing";
  if (path.startsWith("/data-sources")) return "sources";
  if (path.startsWith("/help")) return "help";
  if (path.startsWith("/projects")) return "projects";
  if (path.startsWith("/trials")) return query.get("view") === "saved" ? "saved" : "trials";
  if (path.startsWith("/monitors")) return "monitors";
  if (path.startsWith("/updates")) return "updates";
  return "dashboard";
}

export function globalSearchFromQuery(query: URLSearchParams): { mode: SearchMode; value: string } {
  const ask = query.get("ask");
  if (ask !== null) return { mode: "natural", value: ask };
  return { mode: "keyword", value: query.get("q") ?? "" };
}

export function buildGlobalSearchUrl(value: string, mode: SearchMode): string {
  const trimmed = value.trim();
  if (!trimmed) return "/trials";
  return `/trials?${mode === "natural" ? "ask" : "q"}=${encodeURIComponent(trimmed)}`;
}

export function persistedTheme(value: string | null): "light" | "dark" {
  return value === "dark" ? "dark" : "light";
}

export function persistedLang(value: string | null): "en" | "zh" {
  return value === "zh" ? "zh" : "en";
}

export function persistedSidebar(value: string | null): "expanded" | "collapsed" {
  return value === "collapsed" ? "collapsed" : "expanded";
}
