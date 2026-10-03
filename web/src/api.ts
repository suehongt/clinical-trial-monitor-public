import { useEffect, useState } from "react";
import type { AsyncState } from "./async";

type JsonCacheEntry = {
  data?: unknown;
  fetchedAt?: number;
  pending?: Promise<unknown>;
};

/**
 * Small shared read cache for route-level GETs.
 *
 * Pages in this app are intentionally unmounted when the route changes.  A
 * module-level cache lets a page render its last validated response as soon as
 * the user returns, without keeping every page (and all its effects) mounted.
 */
const jsonCache = new Map<string, JsonCacheEntry>();

// ── localStorage persistence ─────────────────────────────────────────────
// The in-memory map dies with the page, so a reload or a freshly opened
// browser used to send every route back to its loading spinner.  Persisted
// entries let a new page render its previous data immediately.  Search
// results stay sticky (a query snapshot — reopening never silently re-runs
// the query); monitoring surfaces revalidate quietly on mount so freshness
// recovers without loading states.  Writes invalidate the cache, and
// explicit refresh actions use a distinct `?refresh=` URL.
const STORE_PREFIX = "ct-json-cache:";
const STORE_INDEX_KEY = "ct-json-cache:index";
const STORE_MAX_ENTRIES = 40;
/** Per-entry cap — payloads above this stay memory-only. */
const STORE_MAX_BYTES = 512 * 1024;
/** Shared localStorage budget; oldest entries are evicted past it. */
const STORE_TOTAL_BUDGET = 3_500_000;
/** One-off `?refresh=` URLs are never revisited verbatim — memory-only. */
const STORE_SKIP = /[?&]refresh=/;

/**
 * Query snapshots stick across mounts and reloads: reopening a search must
 * repaint the persisted response without re-running the query (its cost on
 * the small demo box is exactly what users felt as "loading every time").
 * Freshness there comes from write invalidation and explicit refresh
 * affordances.  Everything else (dashboard, updates, freshness) revalidates
 * on mount — the server memoizes those reads, so the check is cheap.
 */
function isStickyUrl(url: string): boolean {
  return url.includes("/api/trials/search?");
}

function store(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null; // storage disabled (private mode, sandbox) — memory-only
  }
}

function storeIndex(s: Storage): string[] {
  try {
    const parsed: unknown = JSON.parse(s.getItem(STORE_INDEX_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function writeStoreIndex(s: Storage, urls: string[]): void {
  try {
    s.setItem(STORE_INDEX_KEY, JSON.stringify(urls));
  } catch {
    /* quota — entries remain readable until the tab closes */
  }
}

function loadPersistedCache(): void {
  const s = store();
  if (!s) return;
  for (const url of storeIndex(s)) {
    try {
      const raw = s.getItem(STORE_PREFIX + url);
      if (!raw) continue;
      const entry = JSON.parse(raw) as JsonCacheEntry;
      if (entry && entry.data !== undefined && !jsonCache.has(url)) {
        jsonCache.set(url, { data: entry.data, fetchedAt: entry.fetchedAt });
      }
    } catch {
      /* unreadable entry — dropped, the next fetch rewrites it */
    }
  }
}

function persistEntry(url: string, entry: JsonCacheEntry): void {
  if (entry.data === undefined || STORE_SKIP.test(url)) return;
  const s = store();
  if (!s) return;
  try {
    const payload = JSON.stringify({ data: entry.data, fetchedAt: entry.fetchedAt });
    if (payload.length > STORE_MAX_BYTES) return;
    s.setItem(STORE_PREFIX + url, payload);
    const urls = storeIndex(s).filter((u) => u !== url);
    urls.push(url);
    // Evict oldest entries past the count cap or the shared byte budget;
    // the just-written entry sits last and is never evicted here.
    let total = payload.length;
    for (const u of urls) {
      if (u !== url) total += s.getItem(STORE_PREFIX + u)?.length ?? 0;
    }
    while (urls.length > STORE_MAX_ENTRIES ||
           (total > STORE_TOTAL_BUDGET && urls.length > 1)) {
      const evicted = urls.shift()!;
      total -= s.getItem(STORE_PREFIX + evicted)?.length ?? 0;
      s.removeItem(STORE_PREFIX + evicted);
    }
    writeStoreIndex(s, urls);
  } catch {
    /* quota exceeded — this entry stays memory-only */
  }
}

function dropPersisted(url: string): void {
  const s = store();
  if (!s) return;
  try {
    s.removeItem(STORE_PREFIX + url);
    writeStoreIndex(s, storeIndex(s).filter((u) => u !== url));
  } catch {
    /* ignore */
  }
}

function dropAllPersisted(): void {
  const s = store();
  if (!s) return;
  try {
    for (const url of storeIndex(s)) s.removeItem(STORE_PREFIX + url);
    s.removeItem(STORE_INDEX_KEY);
  } catch {
    /* ignore */
  }
}

loadPersistedCache();

function cachedJson<T>(url: string, guard: (v: unknown) => v is T): T | null {
  const entry = jsonCache.get(url);
  if (!entry || entry.data === undefined || !guard(entry.data)) return null;
  return entry.data;
}

async function fetchJsonShared<T>(url: string, guard: (v: unknown) => v is T): Promise<T> {
  const existing = jsonCache.get(url);
  if (existing?.pending) {
    const data = await existing.pending;
    if (!guard(data)) {
      throw new Error(`Unexpected cached response shape from ${url} (guard ${guard.name || "anonymous"} rejected it)`);
    }
    return data;
  }

  const pending = fetchJson(url, guard);
  jsonCache.set(url, { ...existing, pending });
  try {
    const data = await pending;
    const entry: JsonCacheEntry = { data, fetchedAt: Date.now() };
    jsonCache.set(url, entry);
    persistEntry(url, entry);
    return data;
  } catch (error) {
    // Keep previously rendered data available if a background revalidation
    // fails; the backend health indicator owns the offline/recovery message.
    if (existing?.data !== undefined) jsonCache.set(url, existing);
    else jsonCache.delete(url);
    throw error;
  }
}

/**
 * Url for cached GETs that need a post-write refetch.  `tick` 0 keeps the
 * plain url (persistable in localStorage, shared across pages); any later
 * tick appends `?refresh=N` — a distinct uncached url that forces the fetch
 * and stays memory-only (STORE_SKIP), so one-off post-write urls never
 * evict the persisted snapshots.
 */
export function refreshableUrl(base: string, tick: number): string {
  return tick > 0 ? `${base}${base.includes("?") ? "&" : "?"}refresh=${tick}` : base;
}

/** Drop cached GETs after a write, or a matching subset when appropriate. */
export function invalidateJsonCache(match?: string | RegExp): void {
  if (match === undefined) {
    jsonCache.clear();
    dropAllPersisted();
    return;
  }
  for (const url of jsonCache.keys()) {
    if (typeof match === "string" ? url.startsWith(match) : match.test(url)) {
      jsonCache.delete(url);
      dropPersisted(url);
    }
  }
}

/**
 * Fetch `url` as JSON and validate it with `guard` before handing it over.
 *
 * Throws when the response is not ok (message includes the HTTP status) or
 * when the payload does not satisfy the guard (message includes the URL and
 * the guard that rejected it), so operator drift is diagnosable from logs.
 */
export async function fetchJson<T>(url: string, guard: (v: unknown) => v is T): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status} while fetching ${url}`);
  const raw: unknown = await r.json();
  if (!guard(raw)) {
    throw new Error(`Unexpected response shape from ${url} (guard ${guard.name || "anonymous"} rejected it)`);
  }
  return raw;
}

/** Mutating request (watch add/remove) — the only non-GET calls in the app. */
export async function sendJson<T>(url: string, method: "POST" | "DELETE", guard: (v: unknown) => v is T): Promise<T> {
  const r = await fetch(url, { method });
  if (!r.ok) throw new Error(`HTTP ${r.status} while ${method} ${url}`);
  const raw: unknown = await r.json();
  if (!guard(raw)) {
    throw new Error(`Unexpected response from ${method} ${url}`);
  }
  invalidateJsonCache();
  return raw;
}

/** Typed JSON mutation used by persisted monitoring preferences. */
export async function mutateJson<T>(url: string, method: "POST" | "DELETE" | "PATCH", body: unknown, guard: (v: unknown) => v is T): Promise<T> {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw new Error(`HTTP ${r.status} while ${method} ${url}`);
  const raw: unknown = await r.json();
  if (!guard(raw)) throw new Error(`Unexpected response from ${method} ${url}`);
  invalidateJsonCache();
  return raw;
}

/**
 * React hook that loads `url` into an {@link AsyncState}.
 *
 * - `null` url → `{ status: "idle" }` (nothing requested);
 * - shares validated responses across page mounts until explicitly invalidated;
 * - persists recent responses in localStorage, so a reload or a freshly
 *   opened browser renders the previous data instead of a loading state;
 * - search results are sticky snapshots (no silent re-query on reopen);
 *   every other cached url revalidates in the background while its stale
 *   content stays visible, so freshness recovers without loading states;
 * - never calls setState after unmount or after the url has moved on.
 *
 * `guard` should be a stable module-level function reference: it is captured
 * when the fetch starts (i.e. when `url` changes), so swapping to an inline
 * arrow alone never triggers a refetch.
 */
export function useJson<T>(url: string | null, guard: (v: unknown) => v is T): AsyncState<T> {
  const stateFor = (nextUrl: string | null): AsyncState<T> => {
    if (nextUrl === null) return { status: "idle" };
    const cached = cachedJson(nextUrl, guard);
    return cached !== null ? { status: "ready", data: cached } : { status: "loading" };
  };
  const [state, setState] = useState<AsyncState<T>>(() => stateFor(url));

  // Reset synchronously when the url changes (React's "adjust state during
  // render" pattern) so stale data from the previous url is never painted.
  const [prevUrl, setPrevUrl] = useState(url);
  if (url !== prevUrl) {
    setPrevUrl(url);
    setState(stateFor(url));
  }

  useEffect(() => {
    if (url === null) return; // idle; state already reset above
    const cached = cachedJson(url, guard);
    // Query snapshots stick (isStickyUrl); every other known url paints its
    // cached response instantly and revalidates below — that silent fetch
    // is how freshness recovers after a pipeline run without a loading state.
    if (cached !== null && isStickyUrl(url)) return;
    let current = true;
    fetchJsonShared(url, guard)
      .then((data) => {
        if (current) setState({ status: "ready", data });
      })
      .catch((e: unknown) => {
        // A valid cached response renders above; only uncached urls may
        // surface a full-page error state.
        if (current && !cached) {
          setState({ status: "error", message: e instanceof Error ? e.message : String(e) });
        }
      });
    return () => {
      current = false;
    };
    // deps intentionally [url] only: refetch exclusively when the url changes
  }, [url]);

  return state;
}

export type StickyJson<T> = {
  /** Last successfully loaded payload for this url, or null before the first success. */
  data: T | null;
  /** Error message — only surfaced when there is no data to show instead. */
  error: string | null;
  /** True only before the first successful load (never during a refetch). */
  loading: boolean;
};

/**
 * `useJson` that keeps the last ready payload on screen while a refetch is in
 * flight: post-write `?refresh=` tick bumps and silent mount revalidations
 * must not flash a loading state for data the user is already looking at.
 * The mirror clears when the url goes idle (null) so a page that becomes
 * inapplicable never repaints rows from a stale context.
 *
 * Only use it where every url change is same-resource (a tick bump).  A url
 * that moves to a *different* resource (filters, other entity ids) should
 * stay on `useJson`, which drops the old payload immediately.
 */
export function useStickyJson<T>(url: string | null, guard: (v: unknown) => v is T): StickyJson<T> {
  const state = useJson(url, guard);
  const [sticky, setSticky] = useState<T | null>(() => (state.status === "ready" ? state.data : null));
  useEffect(() => {
    if (state.status === "ready") setSticky(state.data);
    else if (state.status === "idle") setSticky(null);
  }, [state]);
  return {
    data: sticky,
    error: state.status === "error" && sticky === null ? state.message : null,
    loading: sticky === null && state.status === "loading",
  };
}
