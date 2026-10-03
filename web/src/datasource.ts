/**
 * Data-source selection between the live API and the static JSON export.
 *
 * Two modes, one JSON contract (guards.ts validates both):
 *  - "api"    → same-origin FastAPI server (server/app.py) querying the
 *               local database — the self-hosted web platform (Phase 7 P1);
 *  - "static" → the exported JSON files under ./data/ (GitHub-Pages style,
 *               kept as the fallback for environments without a backend).
 *
 * Mode resolution (Phase 3E): the explicit structured health check
 * (./health.ts) is authoritative — "api" only when /api/health answers
 * {status:"ok"} — never inferred from an unrelated request failure.
 * localStorage "ct-datasource" = "api" | "static" remains a manual override
 * (the rollback switch from the project design notes §8).
 * While the backend is offline the app stays browsable in static mode and
 * recovers automatically when the health check flips back to online.
 */

import { useState } from "react";
import { reprobe, useBackendHealth } from "./health";

export type DataSource = "api" | "static";

function forcedMode(): DataSource | null {
  const v = localStorage.getItem("ct-datasource");
  return v === "api" || v === "static" ? v : null;
}
/**
 * React hook: the resolved mode, or null while the first health check is
 * still in flight (treat as "still loading").  Follows health transitions,
 * so monitoring pages recover on their own once the backend is reachable.
 */
export function useDataSource(): DataSource | null {
  const health = useBackendHealth();
  const [forced] = useState<DataSource | null>(forcedMode);
  if (forced) return forced;
  if (health.state === "checking") return null;
  return health.state === "online" ? "api" : "static";
}

/** Convenience re-export so callers can trigger a health retry. */
export { reprobe as retryDataSource };

/* ── URL builders (static paths are exactly the pre-Phase-7 ones) ──────── */

export function indexUrl(mode: DataSource): string {
  return mode === "api" ? "/api/profiles" : "./data/index.json";
}

export function trialsUrl(mode: DataSource, profile: string): string {
  return mode === "api"
    ? `/api/trials?profile=${encodeURIComponent(profile)}`
    : `./data/${profile}.json`;
}

export function mirrorsUrl(mode: DataSource): string {
  return mode === "api" ? "/api/mirrors" : "./data/mirrors.json";
}

export function eventsUrl(mode: DataSource, days: 7 | 30 | 0 = 7): string {
  return mode === "api"
    ? `/api/events?days=${days}&limit=0`
    : "./data/events.json";
}

/** Static: one details file per profile; API: whole-profile details map. */
export function detailsUrl(mode: DataSource, profile: string, file: string): string {
  return mode === "api"
    ? `/api/profiles/${encodeURIComponent(profile)}/details`
    : `./data/${file.replace(".json", "")}_details.json`;
}

/**
 * API only: single-trial detail (Trial shape + TrialDetailData + change
 * history).  The API page uses this instead of the whole-profile details
 * map — one light per-record query beats a 5k-entry extraction, and it
 * carries the change history the static export has no equivalent for.
 */
export function trialDetailUrl(source: string, id: string): string {
  return `/api/trials/${encodeURIComponent(source)}/${encodeURIComponent(id)}`;
}

/**
 * The honest "data as of" stamp: API payloads carry data_as_of (per-source
 * last successful sync — a request timestamp would overstate freshness);
 * static payloads only have the export time in generated_at.
 */
export function asOfText(data: { generated_at: string; data_as_of?: Record<string, string | null> }): string {
  const stamps = Object.values(data.data_as_of ?? {}).filter(
    (v): v is string => typeof v === "string" && v.length > 0,
  );
  if (stamps.length === 0) return data.generated_at;
  return stamps.reduce((a, b) => (a > b ? a : b));
}
