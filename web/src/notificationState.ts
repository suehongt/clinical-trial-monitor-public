import { fetchJson } from "./api";

/** Lightweight Shell-safe unread counter. Keep page modules out of the initial chunk. */
export function refreshUnreadCount(): Promise<number> {
  return fetchJson<{ count: number }>(
    "/api/notifications/unread-count",
    (value): value is { count: number } => typeof value === "object" && value !== null
      && typeof (value as { count?: unknown }).count === "number",
  ).then((data) => data.count);
}
