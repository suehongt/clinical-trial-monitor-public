/**
 * Minimal history-based router (Phase 3E) — no dependency, path + query.
 *
 * Routes (App.tsx maps them to pages):
 *   /dashboard                 landing workspace (redirected from /)
 *   /trials                    trial list (?view=watched, ?profile=key)
 *   /trials/:source/:id        trial detail (?tab=overview|changes|history|sources)
 *   /monitors                  monitor list
 *   /monitors/:id              monitor detail (?tab=overview|trials|activity|runs)
 *   /updates                   updates hub (?view=all|notifications|digest)
 *   /help                      user guide and release notes (?tab=guide|releases)
 *
 * Legacy prototype paths redirect so old links and muscle memory keep
 * working: /watched → /trials?view=watched, /changes → /updates,
 * /notifications → /updates?view=notifications, /timeline → /updates?view=digest,
 * /mirrors → /trials (cross-source mirrors now live in Trial → Sources).
 */

import { useEffect, useState } from "react";

const LAST_ROUTE_KEY = "ct-last-route";

/** Persist only same-origin app paths; never restore API/assets/external URLs. */
function restorableRoute(url: string): boolean {
  return url.startsWith("/") && !url.startsWith("//") &&
    !url.startsWith("/api/") && !url.startsWith("/assets/") && url !== "/";
}

export function rememberRoute(url: string): void {
  if (!restorableRoute(url)) return;
  try {
    localStorage.setItem(LAST_ROUTE_KEY, url);
  } catch {
    /* Storage can be unavailable in private/sandboxed browser contexts. */
  }
}

/** Last useful screen from an earlier browser session, if one is available. */
export function lastRoute(): string | null {
  try {
    const url = localStorage.getItem(LAST_ROUTE_KEY);
    return url && restorableRoute(url) ? url : null;
  } catch {
    return null;
  }
}

export interface Route {
  /** pathname without trailing slash, e.g. "/trials/NCT/NCT123" */
  path: string;
  /** non-empty path segments */
  segments: string[];
  query: URLSearchParams;
  /** full raw url (path + search) */
  url: string;
}

export function parseRoute(): Route {
  const p = window.location.pathname.replace(/\/+$/, "") || "/";
  return {
    path: p,
    segments: p.split("/").filter(Boolean).map(decodeURIComponent),
    query: new URLSearchParams(window.location.search),
    url: window.location.pathname + window.location.search,
  };
}

const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

/** Programmatic navigation; `replace` swaps history instead of pushing. */
export function navigate(to: string, replace = false): void {
  if (replace) history.replaceState(null, "", to);
  else history.pushState(null, "", to);
  rememberRoute(to);
  emit();
}

/** Rewrite the current history entry without emitting a navigation (tab state). */
export function replaceUrl(to: string): void {
  history.replaceState(null, "", to);
}

export function onRouteChange(cb: () => void): () => void {
  const first = listeners.size === 0;
  listeners.add(cb);
  if (first) window.addEventListener("popstate", emit);
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0) window.removeEventListener("popstate", emit);
  };
}

/** React hook: current route, re-parsed on every navigation. */
export function useRoute(): Route {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => onRouteChange(() => setRoute(parseRoute())), []);
  return route;
}

/** Legacy path → product path.  Returns null when the path is current. */
export function redirectPath(path: string, query: URLSearchParams): string | null {
  switch (path) {
    case "/watched": return "/trials?view=watched";
    case "/changes": return "/updates";
    case "/notifications": return "/updates?view=notifications";
    case "/timeline": return "/updates?view=digest";
    case "/mirrors": return "/trials";
    case "/": return "/dashboard";
    default:
      // keep explicit ?view= so /trials?view=watched survives refresh
      return null;
  }
  void query;
}
