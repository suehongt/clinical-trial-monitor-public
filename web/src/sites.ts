/**
 * Trial-site parsing for the detail page's sites table.
 *
 * Sources store `locations` in three shapes:
 *  - JSON array of objects with facility/city/state/country keys
 *    (NCT, CTIS, ISRCTN; individual fields may be null);
 *  - JSON array of plain strings, one facility name each (CTR);
 *  - JSON array of flattened label blobs "国家： … 省(直辖市)： … 单位(医院)： …"
 *    where several sites are concatenated with no separator between them
 *    (ChiCTR). Each blob carries the Chinese and English labels for one site.
 *
 * Everything degrades to a facility-only row rather than dropping sites.
 */

export interface ParsedSite {
  facility: string;
  city: string;
  state: string;
  country: string;
}

/** ChiCTR (bilingual) site labels; a value runs from its colon to the next label. */
const SITE_LABEL_RE =
  /(国家|省[(（]直辖市[)）]|市[(（]区县[)）]|单位[(（]医院[)）]|单位级别|Country|Province|City|Institution hospital|Level of the institution)[：:]/g;

/** A ChiCTR blob starts at each 国家： label; the English Country： label sits mid-site. */
function splitChiCtrBlobs(entry: string): string[] {
  return entry.split(/(?=国家[：:])/).map((x) => x.trim()).filter(Boolean);
}

function normalizeLabel(raw: string): string {
  return raw.replace(/（/g, "(").replace(/）/g, ")");
}

/** label → value (first occurrence wins; empty values stay empty). */
function extractLabeledFields(blob: string): Map<string, string> {
  const fields = new Map<string, string>();
  const marks: Array<{ key: string; valueStart: number; nextStart: number }> = [];
  SITE_LABEL_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = SITE_LABEL_RE.exec(blob))) {
    marks.push({ key: normalizeLabel(match[1]), valueStart: match.index + match[0].length, nextStart: match.index });
  }
  for (let i = 0; i < marks.length; i++) {
    const end = i + 1 < marks.length ? marks[i + 1].nextStart : blob.length;
    const value = blob.slice(marks[i].valueStart, end).trim();
    if (!fields.has(marks[i].key)) fields.set(marks[i].key, value);
  }
  return fields;
}

function parseLabeledBlob(blob: string): ParsedSite {
  const fields = extractLabeledFields(blob);
  const first = (...keys: string[]) => {
    for (const key of keys) {
      const value = fields.get(key);
      if (value) return value;
    }
    return "";
  };
  return {
    facility: first("单位(医院)", "Institution hospital") || blob,
    city: first("市(区县)", "City"),
    state: first("省(直辖市)", "Province"),
    country: first("国家", "Country"),
  };
}

function siteFromObject(value: unknown): ParsedSite {
  const o = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  return {
    facility: String(o.facility ?? o.name ?? ""),
    city: String(o.city ?? ""),
    state: String(o.state ?? o.region ?? ""),
    country: String(o.country ?? ""),
  };
}

/** Plain-text sites: ChiCTR label blobs, "Facility, City, Country" strings,
 *  or a bare facility name (CTR). */
function parsePlainTextSites(entry: string): ParsedSite[] {
  if (/国家[：:]/.test(entry)) {
    return splitChiCtrBlobs(entry).map(parseLabeledBlob);
  }
  const parts = entry.split(/\s*[·|,]\s+/).map((x) => x.trim()).filter(Boolean);
  return [{
    facility: parts[0] || entry,
    city: parts.length > 1 ? parts[parts.length - 2] : "",
    state: "",
    country: parts.length > 1 ? parts[parts.length - 1] : "",
  }];
}

function parseSiteValue(value: unknown): ParsedSite[] {
  if (typeof value === "string") return parsePlainTextSites(value);
  return [siteFromObject(value)];
}

export function parseSites(raw: string[]): ParsedSite[] {
  const sites: ParsedSite[] = [];
  for (const entry of raw) {
    if (!entry.trim()) continue;
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(entry);
    } catch {
      // not JSON — plain text handled below
    }
    if (Array.isArray(parsed)) {
      parsed.forEach((value) => sites.push(...parseSiteValue(value)));
      continue;
    }
    if (typeof parsed === "object" && parsed !== null) {
      sites.push(siteFromObject(parsed));
      continue;
    }
    sites.push(...parsePlainTextSites(entry));
  }
  return sites;
}
