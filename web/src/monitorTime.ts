/**
 * Next-run editing helpers for topic monitors.
 *
 * The scheduler stores next_run_at as a naive UTC "YYYY-MM-DD HH:MM:SS"
 * string (core/monitor_scheduler.py). The monitors list lets the user pin a
 * specific run time via <input type="datetime-local">, which speaks the
 * viewer's wall clock — convert in both directions so what the user picks
 * is what absTime later shows, while storage stays UTC.
 */

const pad = (n: number): string => String(n).padStart(2, "0");

/** Naive UTC stamp → datetime-local value in the viewer's timezone. */
export const utcStampToInputValue = (stamp: string | null | undefined): string => {
  if (!stamp) return "";
  const ms = Date.parse(stamp.includes("T") ? stamp : stamp.replace(" ", "T") + "Z");
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** datetime-local value (viewer-local) → naive UTC stamp for PATCH next_run_at. */
export const inputValueToUtcStamp = (value: string): string | null => {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)) return null;
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) return null;
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
};
