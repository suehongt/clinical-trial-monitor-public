import type { Translate } from "./i18n";

export const SOURCE_BADGES: Record<string, { label: string; color: string }> = {
  NCT: { label: "ClinicalTrials.gov", color: "#2563eb" },
  ChiCTR: { label: "ChiCTR", color: "#dc2626" },
  CTR: { label: "CTR (NMPA)", color: "#ca8a04" },
  ICTRP: { label: "WHO ICTRP", color: "#16a34a" },
  CTIS: { label: "EU CTIS", color: "#7c3aed" },
  ISRCTN: { label: "ISRCTN", color: "#0891b2" },
  EUCTR: { label: "EUCTR (historical)", color: "#9333ea" },
};

const STATUS_TONE: Record<string, string> = {
  Recruiting: "tone-green",
  "Not yet recruiting": "tone-blue",
  "Active, not recruiting": "tone-blue",
  Completed: "tone-slate",
  Terminated: "tone-red",
  Suspended: "tone-orange",
  Withdrawn: "tone-gray",
  Unknown: "tone-gray",
};

export function statusTone(status: string): string {
  return STATUS_TONE[status] ?? (status.includes("Recruit") ? "tone-green" : "tone-gray");
}

/** Collapse Chinese/English status variants for the status filter. */
export function statusKey(status: string): string {
  // Check explicit non-recruiting phrases before the positive substring:
  // both English labels below contain "recruiting", and Chinese negative
  // labels commonly retain the word 招募 as well.
  if (/Not yet recruiting|Active,\s*not recruiting|尚未招募|未开始招募|不再招募|招募(?:结束|完成|停止|暂停)/i.test(status)) {
    return "other";
  }
  if (/Recruiting|招募|正在进行/i.test(status)) return "recruiting";
  if (/Completed|完成/i.test(status)) return "completed";
  if (/Terminat|终止|停止/i.test(status)) return "terminated";
  return "other";
}

/**
 * Canonical phase buckets — mirrors core.monitors.normalize_phase so the UI
 * renders the same buckets the search facets count and the phase filter
 * matches.  Display only: the server stays the matching authority.
 */
const PHASE_JOIN = /(phase\s*(iv|iii|ii|i|Ⅳ|Ⅲ|Ⅱ|Ⅰ|0|1|2|3|4))\s*(?:\/|&|,|-|to\b)\s*(?=(iv|iii|ii|i|Ⅳ|Ⅲ|Ⅱ|Ⅰ|0|1|2|3|4)\b|phase\s*(iv|iii|ii|i|Ⅳ|Ⅲ|Ⅱ|Ⅰ|0|1|2|3|4))/gi;
const PHASE_MAIN = /phase\s*(iv|iii|ii|i|Ⅳ|Ⅲ|Ⅱ|Ⅰ|0|1|2|3|4)\b/gi;
const PHASE_CJK = /([ⅣⅢⅡⅠ])\s*期|([0-4])\s*期/g;
const PHASE_NUMBERS: Record<string, string> = {
  i: "1", ii: "2", iii: "3", iv: "4", "Ⅳ": "4", "Ⅲ": "3", "Ⅱ": "2", "Ⅰ": "1",
};
const PHASE_NA = new Set(["n/a", "na", "not applicable", "not selected", "unknown", ""]);

export function normalizePhase(raw: string | null | undefined): string {
  const text = (raw ?? "").trim();
  if (PHASE_NA.has(text.toLowerCase())) return "N/A";
  const numbers: string[] = [];
  const push = (token: string) => {
    const n = PHASE_NUMBERS[token] ?? PHASE_NUMBERS[token.toLowerCase()] ?? PHASE_NUMBERS[token.toUpperCase()] ?? token;
    if (!numbers.includes(n)) numbers.push(n);
  };
  const joined = text.replace(PHASE_JOIN, (_m, g1: string) => g1 + " phase ");
  for (const m of joined.matchAll(PHASE_MAIN)) push(m[1]);
  for (const m of text.matchAll(PHASE_CJK)) push(m[1] ?? m[2]);
  if (numbers.length === 0) return "Other";
  return numbers.length === 1 ? `Phase ${numbers[0]}` : "Phase " + numbers.join("/");
}

/** Localized label for a (possibly raw) phase value; raw passes through. */
const PHASE_LABEL_KEY: Record<string, Parameters<Translate>[0]> = {
  "Phase 0": "search.phase.p0", "Phase 1": "search.phase.p1",
  "Phase 1/2": "search.phase.p1_2", "Phase 1/2/3/4": "search.phase.p1_2_3_4", "Phase 2": "search.phase.p2",
  "Phase 2/3": "search.phase.p2_3", "Phase 3": "search.phase.p3",
  "Phase 3/4": "search.phase.p3_4", "Phase 4": "search.phase.p4",
  "N/A": "card.phaseNa", "Other": "search.phase.other",
};

export function phaseLabel(phase: string | null | undefined, t: Translate): string {
  if (!phase || phase === "N/A") return t("card.phaseNa");
  // Bucket first so raw registry spellings ("Phase 1/Phase 2", ICTRP
  // combination strings, Ⅱ期) render as the same canonical label the
  // facets offer; anything unbucketable passes through untouched.
  const key = PHASE_LABEL_KEY[normalizePhase(phase)];
  return key ? t(key) : phase;
}
