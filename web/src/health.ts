/**
 * Explicit backend health/capability detection (Phase 3E).
 *
 * The old datasource probe fired once per page load with a 3s timeout and
 * degraded to static mode for the whole session on any failure — the root
 * cause of the dead-end "Monitoring features require the local server."
 * screen.  This module is the single structured source of truth instead:
 *
 *  - "online"          — GET /api/health answered 200 with status "ok"
 *                        (never inferred from an unrelated request failure);
 *  - "offline"         — no answer at all (fetch error/timeout/non-JSON);
 *  - "database_error"  — the API answered 503: backend up, database broken;
 *  - "checking"        — probe in flight.
 *
 * State is module-level (one probe shared by every component) with
 * subscriptions; while offline it re-probes automatically on window focus
 * and on a slow interval, plus on every `reprobe()` (the Retry button).
 */

import { useEffect, useState } from "react";

export type HealthState =
  | "checking"
  | "online"
  | "offline"
  | "database_error";

export interface BackendHealth {
  state: HealthState;
  appVersion?: string;
  /** schema_version from /api/health (capability signal; null when absent) */
  schemaVersion: number | null;
  lastChecked: string | null;
  error: string | null;
}

let state: BackendHealth = { state: "checking", schemaVersion: null, lastChecked: null, error: null };
const subscribers = new Set<() => void>();
let inflight: Promise<void> | null = null;
let offlineTimer: ReturnType<typeof setInterval> | null = null;

function set(next: BackendHealth): void {
  state = next;
  for (const s of subscribers) s();
  scheduleOfflinePolling();
}

function scheduleOfflinePolling(): void {
  const need = state.state === "offline" || state.state === "database_error";
  if (need && offlineTimer === null) {
    offlineTimer = setInterval(() => void reprobe(), 15000);
  } else if (!need && offlineTimer !== null) {
    clearInterval(offlineTimer);
    offlineTimer = null;
  }
}

async function probe(): Promise<void> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const r = await fetch("/api/health", { signal: controller.signal });
    let body: unknown = null;
    try {
      body = await r.json();
    } catch {
      // non-JSON answer — treat below by status code
    }
    const obj = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
    if (r.ok && obj.status === "ok") {
      set({
        state: "online",
        appVersion: typeof obj.app_version === "string" ? obj.app_version : undefined,
        schemaVersion: typeof obj.schema_version === "number" ? obj.schema_version : null,
        lastChecked: new Date().toISOString(),
        error: null,
      });
      return;
    }
    if (r.status === 503) {
      set({ state: "database_error", schemaVersion: null, lastChecked: new Date().toISOString(), error: null });
      return;
    }
    // reachable but unhealthy / unexpected payload
    set({ state: "offline", schemaVersion: null, lastChecked: new Date().toISOString(), error: `HTTP ${r.status}` });
  } catch (e: unknown) {
    set({
      state: "offline",
      schemaVersion: null,
      lastChecked: new Date().toISOString(),
      error: e instanceof Error ? e.message : String(e),
    });
  } finally {
    clearTimeout(timer);
  }
}

/** Force a fresh health probe (idempotent — concurrent calls share one). */
export function reprobe(): Promise<void> {
  inflight ??= probe().finally(() => {
    inflight = null;
  });
  return inflight;
}

/** Kick the first probe off at module load so state lands ASAP. */
void reprobe();

/** React hook: the shared backend health state. */
export function useBackendHealth(): BackendHealth {
  const [snapshot, setSnapshot] = useState(state);
  useEffect(() => {
    const update = () => setSnapshot(state);
    subscribers.add(update);
    update();
    const onFocus = () => void reprobe();
    window.addEventListener("focus", onFocus);
    return () => {
      subscribers.delete(update);
      window.removeEventListener("focus", onFocus);
    };
  }, []);
  return snapshot;
}
